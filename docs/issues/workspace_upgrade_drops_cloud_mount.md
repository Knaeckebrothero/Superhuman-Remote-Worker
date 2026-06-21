# Workspace tier upgrade drops the OpenCloud cloud mount (agent loses the data it upgraded to work on)

**Filed:** 2026-06-21, from the `sandbox → vm` completion test (thread `16a8613d`, agent
`sha-a8da5dd`) while verifying [[workspace_tier_upgrade]]. The upgrade itself succeeded
end-to-end (register → SSH → seed → swap → retool → `sudo`=root), but the upgraded VM had
**no cloud mount** — the user's OpenCloud Personal Space (`cloud/`) was gone.

**Severity:** High. The whole point of an on-demand upgrade is to keep working on the
*current* task with more power (shell / sudo). Silently losing access to the live cloud data
the agent was processing — at the exact moment it needed the upgrade — defeats the feature
and undercuts the "deep cloud integration" product thesis ([[project_cloud_storage_thesis]]).

> **Investigation note (2026-06-21):** a codebase trace + web/security research corrected **two**
> assumptions in the first draft of this issue: (a) the VM image does **not** lack rclone/fuse3 —
> they're already baked in via the Packer provision script; and (b) the fix is **not** "use the
> public WebDAV URL (already adopted)" — the code deliberately uses the *internal* URL, and the
> real fix is to make that choice **topology-aware**. Both corrections are reflected below.

---

## Recommendation (TL;DR)

**Re-establish the cloud mount on the new backend as part of the upgrade** (not a warning). The
machinery is mostly already there — rclone/fuse3 are in the VM image, tokens are minted
agent-side, the mount is backend-agnostic. Two real code changes + one decision:

1. **Handler — re-mount after the swap (small).** In `_handle_workspace_upgrade`, after
   `swap_backend()` and **before** `resetup_tools_for_backend()`, re-run
   `_setup_cloud_mount(cloud_mount_cfg)` against the new backend (stop the stale manager first).
   `_setup_cloud_mount` is already re-callable — it binds the manager to the *current*
   `workspace_manager.backend` (`persistent_session.py:441`), which `swap_backend()` repoints to
   the VM. Order matters: `srw_cloud_status` tool exposure is gated on `cloud_mount_manager.active`
   (`:648`), so remount → then retool.
2. **OpenCloud adapter — make the mount WebDAV URL topology-aware (the real VM blocker, medium).**
   `build_rclone_mount_spec` hardcodes the **internal** URL (`http://srw-opencloud:9200/…`,
   `opencloud.py:193-204`) — unreachable from the separate `vm` cluster. Thread a "cross-cluster VM
   runtime" signal from `_build_agent_cloud_mount` (`main.py:14553`, already reads `metadata["vm"]`)
   so VM-tier mounts use the **public** URL (`self._public_url`, port 443, which the VM egress
   allows) while sandbox-pod mounts keep the internal URL (status quo — no hairpin, k3d-safe).
3. **Decision — read-only by default on the `vm` tier** (`access == "read_only"` is already
   supported, `cloud_mount/__init__.py:552-553`). A `vm` tier is **root**; see § Security.

Plus: correct the now-false `seed.py:59-60` comment. **No VM-image or KubeVirt-template change is
needed** (rclone/fuse3 already present; the guest provides `/dev/fuse` natively).

---

## Symptom

A `sandbox` session with an active OpenCloud mount runs `/upgrade-workspace vm`. The upgrade
completes and `sudo` works — but on the VM:

- the workspace files carry over (the seed copied 79 files), **but**
- `cloud/` (the OpenCloud Personal Space) is **absent** — not seeded, not re-mounted.

**Empirical proof (2026-06-21, thread `16a8613d`):**
- Sandbox workspace pod had `srw-16a8613d-home on /cloud/home type fuse.rclone` and a
  `cloud -> /cloud/home` symlink in `/home/agent-host/workspace`.
- During the upgrade the agent logged `Seed: skipping unreadable entry 'cloud'` (correct) and
  then **zero** rclone / cloud-mount activity — no remount was ever attempted on the VM.
- The cockpit "Workspace ready" banner says "79 file(s) carried over" but never mentions the
  lost mount.

---

## Root cause

Two independent gaps. **Both** must be fixed for `→ vm`; gap #1 alone fixes `→ sandbox`.

### 1. The upgrade handler never re-establishes the mount

The cloud mount is **agent-driven**, not a network route from the orchestrator. `RcloneMountManager`
(`src/services/cloud_mount/__init__.py:157` — "Start and stop rclone mounts in a remote workspace
runtime") SSHes into the workspace via `workspace_backend.exec_command` (`:419`) and runs
`rclone mount` on the host (`_run_remote_script("mount_srw-<tid>-…sh")`; remote name
`srw-{thread_id[:8]}-…`, `:439`). Because it goes through the backend, it is **backend-agnostic** —
it works on *any* RemoteBackend, pod or VM.

It is wired up **once at session boot**: `persistent_session._setup_cloud_mount()`
(`persistent_session.py:427`) builds an `RcloneMountManager` bound to the *current*
`workspace_manager.backend` (`:438,:441`) and calls `start_all()` (`:444`), invoked from
`_attach_session` (`:246`).

`_handle_workspace_upgrade` (`src/api/persistent_app.py` ≈ `:4706-4828`) does:
`poll vm ready → build RemoteBackend → connect → seed (skip cloud) → swap_backend() →
resetup_tools_for_backend() → re-open sudo gate → persist tier → workspace_upgrade.complete`.
There is **no `_setup_cloud_mount` call** anywhere in it. So after the swap, `cloud_mount_manager`
is still bound to the **old, now-disconnected** sandbox backend, and nothing runs `rclone mount`
on the new one. `_setup_cloud_mount` is re-callable (binds to the *current* backend) — the call
site is simply missing; it doesn't stash `cloud_mount_cfg` or stop the prior manager, so a re-call
needs the cfg passed back in. (The *"safe to call again post-swap"* docstring at `:613-623` is for
the sibling `_load_tools_for_backend`, which reads `cloud_mount_manager` to expose
`srw_cloud_status` at `:648` — hence remount-then-retool ordering.)

> General to all upgrade targets, not just VM: a `virtual → sandbox` upgrade lands on a pod that
> *can* mount but the handler never starts it. Most visible for `→ vm`, where gap #2 also bites.

### 2. The mount WebDAV URL is the internal cluster service — unreachable from the VM cluster

`build_rclone_mount_spec` **deliberately** reconstructs the rclone source URL from the **internal**
base URL and rejects the public one (`orchestrator/services/cloud/opencloud.py:193-204`):

> *"Always reconstruct from the internal base URL. The persisted vendor_meta webdav_url … is the
> PUBLIC URL — mounting that would hairpin all rclone traffic through the public edge (and fails on
> local k3d, where workspace pods cannot reach it at all)."*

So the mount URL is `{self._base_url}/dav/spaces/{native_id}/`, where `_base_url` = `OPENCLOUD_URL`
= **`http://srw-opencloud:9200`** (internal service; `helm/templates/configmap.yaml:210`). A
separate `self._public_url` (`OPENCLOUD_PUBLIC_URL` = `cloud.<domain>`, `configmap.yaml:211`) is
used only for browser/web URLs — **never** the mount. Session-folder URLs are built the same
internal way (`opencloud.py:1224-1232`).

This is correct for a **sandbox pod** (same cluster as `srw-opencloud` → resolves + routes). But a
**VM is on the separate `vm` cluster** (`helm-vm-cluster/values.yaml:5-17` — the two clusters join
only via the NATS hub + Headscale mesh). The VM **cannot resolve or route to `srw-opencloud:9200`**:
the service DNS isn't reachable cross-cluster, and port 9200 isn't in the VM egress allow-list
anyway. The VM egress NetworkPolicy (`helm-vm-cluster/templates/vm-controller/network-policy.yaml:46-50`)
allows **80/443 to anywhere** + DNS(53) + Tailscale + NATS 4222 — so the **public HTTPS URL (443)
*is* reachable**, but the internal `:9200` is doubly blocked. Hence: even with gap #1 fixed, the
VM would mount a URL it can't reach.

**Not blockers (already handled — corrected from the first draft):**
- **rclone + fuse3 are already in the VM image.** Not in the `Dockerfile.containerDisk` wrappers
  (they just `COPY` a Packer-built qcow2) but in `docker/agent-vm-base/scripts/provision-stage1.sh`
  (`:124` fuse3; `:171-175` rclone **1.74.3**, same checksum as `docker/Dockerfile.workspace`).
  Listed DONE in `docs/features/rclone_cloud_mount.md:647,1221`.
- **FUSE works natively in the KubeVirt guest** (`/dev/fuse` from the guest kernel) — no template
  device entry, no `SYS_ADMIN` (that's a *container* problem). The mount script's `sudo mkdir`
  fallback + `agent-host` NOPASSWD (`provision-stage2.sh:54`) cover dir creation.
- **Tokens are minted agent-side**, refreshed in a loop, and pushed to the workspace as a
  `bearer.token` read by a `bearer_token_command` helper (`cloud_mount/__init__.py:280-378,:896-914`).
  The VM never contacts Keycloak, so the internal Keycloak issuer is irrelevant.
- **The orchestrator already builds a `cloud_mount` payload for a ready VM**
  (`_runtime_supports_rclone_mount` true when `vm.status==ready && vm.ssh_host`,
  `main.py:14429-14442`; surfaced in `_poll_workspace_ready`'s vm branch, `persistent_app.py:4541`).

---

## Scope / blast radius

| Path | Mount before | After upgrade (today) | Gaps |
|---|---|---|---|
| Session `sandbox → vm` | ✅ active rclone mount | ❌ **lost** (confirmed) | #1 + #2 |
| Session `virtual → vm` | lite: no shell mount (webdav tools) | ❌ none on VM | #1 + #2 |
| Session `virtual → sandbox` | lite: no shell mount | ❌ likely none, **unverified** | #1 only (pod *can* mount) |
| Worker `virtual/none → sandbox` | lite | ❌ likely none, **unverified** | #1 only |

Confirmed/demonstrated: `sandbox → vm`. The others share the missing-remount root cause (#1) and
should be verified as part of the fix.

---

## Proposed fix

1. **Re-mount during the swap (`_handle_workspace_upgrade`).** After `swap_backend(new_backend)`
   and **before** `resetup_tools_for_backend()`:
   - `await _session.cloud_mount_manager.stop_all()` if one is active (it's bound to the dead old
     backend);
   - `await _session._setup_cloud_mount(cloud_mount_cfg)` → fresh manager binds to the VM backend,
     `start_all()` mounts on the VM; then retool so `srw_cloud_status` is exposed.
   - Source `cloud_mount_cfg`: stash it on `_session` at boot, or re-fetch from workspace status
     (`ws.get("cloud_mount")`, `persistent_app.py:4541`).
   - Non-fatal: on mount failure, log + emit `workspace_upgrade.cloud_mount_degraded`; don't abort
     the otherwise-successful upgrade.

2. **Topology-aware mount URL (`opencloud.py`).** Thread a `runtime_kind` / `use_public_url` flag
   from `_build_agent_cloud_mount` (`main.py:14553`, which already inspects `metadata["vm"]`)
   through the `_build_rclone_*` helpers into `build_rclone_mount_spec` (`opencloud.py:154`) and
   `get_session_folder_webdav_url` (`:1224`). VM-tier → `self._public_url`; sandbox-pod → `self._base_url`
   (unchanged). Keep the per-Space path (`/dav/spaces/{id}/[subpath]`) — see § Security. The tus
   data-gateway hop *already* uses the public URL for writes (`opencloud.py:234-238`), so switching
   the whole VM mount to public is consistent, not a new exposure. TLS: on dev/prod the public URL
   has a valid cert; k3d has no KubeVirt so VM mounts never run there (the `--no-check-certificate` /
   `mount_insecure_tls` path stays a local-only knob).
   **Do not** flip the URL globally — that re-introduces the hairpin / k3d-unreachable failure the
   internal-URL choice exists to prevent. It must be **VM-conditional**.

3. **No VM-image / KubeVirt-template change.** rclone/fuse3 already baked in; `/dev/fuse` native.
   Just delete the (now-false) "VM image lacks rclone" assumption wherever it was repeated.

4. **Fix the seed comment** (`src/core/backends/seed.py:59-60`). It claims *"the cloud mount is
   RE-mounted on the new backend"* — false until fix #1 lands. Reword: skipped because it's a **live
   mount, not copyable**; re-established by the handler re-running `_setup_cloud_mount` post-swap.

---

## Security (decide before shipping) — informed by the cloud-mount research

A `vm` tier is **root**, and the cloud shell-guardrails (`shell_tools.py:73-78`,
`cloud_mount/guardrails.py`) gate only the *agent's tools*, not a root user bypassing them. So
mounting the user's cloud into a root VM is a real blast-radius increase. The complication:
**OpenCloud is OIDC-bearer-only** — unlike Nextcloud it has **no app-passwords, no read-only token,
no folder-scoped token** ([OpenCloud authz docs](https://docs.opencloud.eu/docs/dev/server/apis/http/authorization/)).
So least-privilege can't be enforced server-side; the real levers are:

- **Mount-URL scoping (primary).** OpenCloud serves per-Space WebDAV (`/dav/spaces/{id}/[subpath]`),
  which we already use. Mounting just the relevant **Space / subfolder** (not the whole drive)
  bounds the damage *surface* — the highest-leverage control, and server-honored.
- **Token lifetime (already in place).** Tokens are short-lived, agent-minted, refreshed via a
  command helper, and revocable at session end — matching the "fresh scoped token" model of
  Codespaces / the Coder `GIT_ASKPASS` broker. Keep it; don't bake a long-lived token into the VM.
- **`--read-only` by default on the vm tier** (`access == "read_only"`, `__init__.py:552-553`).
  Caveat: it's *client-side*, so it's defense-in-depth, **not** a hard wall against root (root can
  remount RW with the same token). Still the right default; RW becomes an explicit, audited escalation.

**Recommended default:** Space/subfolder-scoped + short-lived token (status quo) + **read-only**,
with read-write as a deliberate per-task escalation. **Strongest (optional v2):** run rclone
host/sandbox-side and re-export only the scoped subtree into the VM (virtiofs / `rclone serve`), so
the root VM never holds a cloud credential at all — the "authority without possession" pattern. More
work; revisit if a hard boundary against root is required.

---

## rclone-in-VM gotchas to bake in (from the research)

- **`--vfs-cache-mode full`** — WebDAV can't stream uploads (rclone warns); `full` makes the remote
  behave like a local disk (app-compat). Cache lands on the guest disk → size the VM disk for
  `--vfs-cache-max-size` (+ sparse-file support).
- **`vendor = infinitescale`** — already set (`opencloud.py:231`); OCIS/OpenCloud tus uploads.
- **`bearer_token_command`, not a static token** — already the model; OpenCloud tokens expire in
  minutes.
- **systemd unit from cloud-init** with **absolute** `--config`/`--cache-dir` paths (systemd has no
  `$HOME`), `After=network-online.target`, `Type=notify`, and `ExecStop=fusermount3 -uz <mnt>`
  (lazy unmount avoids "busy" failures on teardown). AppArmor/SELinux can silently block FUSE —
  check `dmesg`/`ausearch` if `fusermount3: Permission denied`.

---

## Test plan (acceptance)

Re-run `sandbox → vm` on dev (the [[workspace_tier_upgrade]] recipe); on the VM post-upgrade assert:
1. `mount | grep fuse.rclone` shows the mount (e.g. `srw-<tid>-home on /cloud/home`);
2. `ls cloud/` lists the user's OpenCloud Space contents;
3. a file the agent had open pre-upgrade is readable at the same `cloud/...` path after;
4. if RO: a write is refused; if RW: a write round-trips to OpenCloud;
5. agent logs show `_setup_cloud_mount` against the **VM** backend (host = mesh IP) over the
   **public** WebDAV URL;
6. teardown clean (no leaked rclone process / mount on session end).
Pre-flight smoke from inside an `agent-vms` VM: `curl -sI https://cloud.<dev-domain>/` resolves +
trusted cert; `modprobe fuse; ls /dev/fuse`. Then verify `virtual → sandbox` / `virtual → vm` too.

---

## Interim mitigation (until fixed)

Optional stopgap (the ask is the fix, not a warning): when a cloud mount was active, have
`workspace_upgrade.complete` append a one-line notice that the mount isn't on the VM yet (tracked
here), so the loss isn't silent. Remove once the fix ships.

---

## References

- Feature: `docs/features/workspace_tier_upgrade.md` ([[project_workspace_tier_upgrade]]).
- Cloud access model: `docs/features/no_workspace_agent_mode.md`; mount internals:
  `docs/features/rclone_cloud_mount.md` (internal-URL rationale `:948-952,:977-982`; VM-image DONE
  `:647,:1221`); [[project_opencloud_rclone_mounts]], [[project_rclone_dev_cluster_test]].
- **The URL fix site:** `orchestrator/services/cloud/opencloud.py` (internal-URL choice `:193-204`,
  session-folder URL `:1224-1232`, `_public_url` `:103`, insecure-TLS flag `:234-239`);
  `orchestrator/main.py` (`_runtime_supports_rclone_mount` `:14429-14442`, `_build_agent_cloud_mount`
  `:14553`, `_build_rclone_*` `:14461/:14514`); `helm/templates/configmap.yaml` (`OPENCLOUD_URL`
  internal `:210`, `OPENCLOUD_PUBLIC_URL` `:211`).
- **Handler / mount:** `src/api/persistent_app.py` `_handle_workspace_upgrade` (`:4706-4828`),
  `cloud_mount` source `:4541`; `src/api/persistent_session.py` `_setup_cloud_mount` `:427-452`,
  `swap_backend` rebind `:1005`, retool gate `:648`; `src/services/cloud_mount/__init__.py`
  (backend-agnostic mount `:419`, token mint/refresh `:280-378`, RO support `:552-553`, sudo
  fallback `:579-581`); `src/core/backends/seed.py:54-64`.
- **Already-present (no work):** `docker/agent-vm-base/scripts/provision-stage1.sh` (fuse3 `:124`,
  rclone `:171-175`); KubeVirt egress `helm-vm-cluster/templates/vm-controller/network-policy.yaml:46-50`.
- **Security/prior-art:** OpenCloud OIDC-only (no scoped tokens) —
  https://docs.opencloud.eu/docs/dev/server/apis/http/webdav/ ; credential-proxy / host-side
  re-export patterns (Codespaces, Coder broker, E2B/Daytona microVM volume mounts).
