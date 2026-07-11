# Protected Cloud Mode — Phase 1, Slice B (Workspace Mount Stack) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a protected session actually mount — a read-only rclone lower (using Slice A's per-mount reader grant, not `agent-service`) with a fuse-overlayfs capture overlay stacked on top — such that all agent writes land in a local upperdir, the mount survives refresh/heal/pod-churn, and the staged upperdir is captured by snapshots.

**Architecture:** This slice spans three subsystems. (1) **Backend** (`orchestrator/services/cloud/nextcloud.py`): cure the canary fixture so Slice A's fail-closed engage gate can pass against a live Nextcloud (real version/trash ids, not synthetic). (2) **Agent-side mount plumbing** (`src/services/cloud_mount/`, new `src/services/cloud_overlay/`): a new `OverlayMountManager` (sibling to `RcloneMountManager`) that mounts fuse-overlayfs over the RO rclone lower, a `vfs/refresh` method on `RcloneMountManager`, an ENOTCONN health monitor + heal, a bulk-delete guard, and an upperdir quota guard — all generated as bash scripts run over the existing workspace SSH channel and unit-tested by asserting script text (the established `FakeRemoteBackend` pattern). (3) **Orchestrator wiring** (`orchestrator/main.py`, `orchestrator/services/snapshot_service.py`, `orchestrator/services/ssh_helpers.py`): call `engage_ro_mount` once at thread-create for a protected session, build the RO+overlay `cloud_mount` payload from the `cloud_ro_mounts` row, and add `--xattrs`/`--acls` to the snapshot capture/extract so overlay whiteouts round-trip. No Cockpit, no review/apply UI, no `DiffSource` — those are Slice C.

**Tech Stack:** Python 3.12, `httpx` + `httpx.MockTransport` fakes (backend), bash-script generation run via `workspace_backend.exec_command`/`write_home_file` over paramiko SSH (agent-side), `pytest` + `pytest-asyncio`, `unittest.mock` (`AsyncMock`/`patch`) for orchestrator/snapshot paths. fuse-overlayfs ≥ 1.13 and rclone (already in `docker/Dockerfile.workspace`).

## Global Constraints

- **Protected mode v1 is Nextcloud-only** (design §9.2 decision, commit `6b943b2b`): OpenCloud is dropped from protected mode. Every protected-path branch keys off a Nextcloud-backed `cloud_ro_mounts` row; do not wire OpenCloud's `SupportsRoReader` code into any of this.
- **Protected mode v1 is container/pod-runtime-only.** The reader `webdav_url` persisted by Slice A (`mint_ro_grant`) is the **internal** Nextcloud URL; a cross-cluster VM runtime can't reach it. When `metadata.vm.status == "ready"`, protected engage must refuse (fail-closed: no mount, degraded flag) rather than mount a broken lower. VM-tier protected mode is deferred.
- **Fail-closed everywhere.** If `engage_ro_mount` raises `RoEngageRefused`, the session must NOT fall back to a live/unprotected mount — it boots with no cloud mount and a `protected_cloud_error` surfaced to the agent. A protected session that can't prove read-only gets no cloud access, never live access.
- **The overlay layout is fixed by the snapshot placement rule (design §11.3, §11.6 amendment #6):** upperdir + workdir **inside** `/home/agent-host/` (captured); the merged overlay mountpoint **and** the raw rclone RO lower **outside** `/home/agent-host/` (not captured). Concretely: lower `= /cloud/lower`, upper `= /home/agent-host/.overlay/upper`, work `= /home/agent-host/.overlay/work`, merged `= /cloud/merged`; the agent's `workspace/cloud` symlink points at `/cloud/merged`.
- **Refresh uses `vfs/refresh`, never `vfs/forget`** (design §11.2, §11.6 amendment #3): `vfs/forget` does not flush already-read file content; only `vfs/refresh` (or a full rclone remount) does. Held FDs across a refresh get silent stale reads, so the refresh op **must** quiesce first and use a plain `fusermount3 -u` (which EBUSYs while FDs are held) as the guard — never a lazy `-uz` on the refresh path.
- **Heal uses overlay-first lazy unmount** (design §11.2): on rclone-lower death (detected only by an ENOTCONN read-probe — `/proc/mounts` and `mountpoint -q` both lie over a dead endpoint), unmount the overlay with `-uz` FIRST, then the dead lower, then remount both. Lazy `-uz` is safe here (unlike refresh) because the dead lower makes every held-FD read fail loudly with ENOTCONN.
- **fuse-overlayfs option string is exactly `lowerdir=<lower>,upperdir=<upper>,workdir=<work>`** (Phase 0 spike `scripts/spike/overlay_matrix.sh:92`) — no `metacopy`, `redirect_dir`, `index`, etc. Do not add overlay options.
- **Work on `develop` directly; commit per task; do NOT push without asking** (project convention).
- **CI is the gate, not local pytest.** CI runs on Python 3.12 with **no `/dev/fuse`, no privileged containers, no live cloud backend**. Every test here must pass with mocked `httpx`/SSH-backend. Live NC validation of engage + a real overlay mount is a documented **manual** step (design §11.4), not a CI test.
- **`schema_current.sql` is untouched:** Slice B adds no table (the `cloud_ro_mounts` table + CRUD landed in Slice A). Do not add a migration.

---

## File Structure

**New files:**
- `src/services/cloud_overlay/overlay_mount.py` — `OverlayMountManager`: mounts/unmounts fuse-overlayfs over the RO rclone lower, owns the `workspace/cloud → merged` symlink, refresh/heal, and upperdir-usage probe. Sibling to `RcloneMountManager`; runs bash over `workspace_backend`. Tasks B2–B4, B6.
- `tests/cloud_overlay/test_overlay_mount.py` — script-text + mocked-loop tests for `OverlayMountManager`. Tasks B2–B4, B6.
- `tests/cloud/test_ro_canary_nextcloud.py` — mocked-httpx tests for the cured canary. Task B1.

**Modified files:**
- `orchestrator/services/cloud/nextcloud.py` — `seed_canary_fixture` enumerates real version/trash ids; `_enumerate_canary_refs` helper. Task B1.
- `src/services/cloud_mount/__init__.py` — `refresh_vfs()` method (B3); `_start_all_sync` honors `skip_workspace_links` (B9).
- `src/services/cloud_mount/guardrails.py` — `detect_cloud_delete_risk` for bulk deletes (B5); `_CLOUD_ROOTS` gains `/cloud/merged`/`/cloud/lower` coverage implicitly (already prefix-matched).
- `src/tools/shell/shell_tools.py` — wire the bulk-delete guard + upperdir-cap guard into the shell preflight (B5, B6).
- `orchestrator/services/snapshot_service.py` — `--xattrs --acls --xattrs-include=*` on the capture tar (B7).
- `orchestrator/services/ssh_helpers.py` — `--xattrs --acls --xattrs-include=*` on `EXTRACT_REMOTE_CMD` (B7).
- `orchestrator/main.py` — `ThreadCreateRequest.protected_cloud` field + persist marker; `_engage_protected_cloud_for_thread` called at create; `_build_protected_cloud_mount` payload builder + protected branch in `_build_agent_cloud_mount` (B8).
- `src/api/persistent_session.py` — `_setup_cloud_mount` stacks the overlay after the RO lower; teardown; status (B9).
- `tests/test_snapshot_ssh_extraction.py` — update the pinned `EXTRACT_REMOTE_CMD` assertion (B7).
- `tests/test_thread_mount_rows.py` — protected-payload coverage (B8).
- `tests/cloud_mount/test_rclone_mount_manager.py` — `refresh_vfs` + `skip_workspace_links` coverage (B3, B9).

---

### Task B1: Cure the Nextcloud canary fixture (real version/trash ids)

Slice A's `seed_canary_fixture` returns `CanaryFixture(path, version_ref=None, trash_ref=None)` — so the RO probe's CVE side channels stay `inconclusive` and the engage gate **refuses every real run** (`ro_engage.py:10-13`). This task enumerates the canary file's **real** version id and a **real** trashed-item id so `side_channel_probes` targets ids the server actually knows, turning `inconclusive` into a verified `403` (rejected) on a correctly-RO reader. Design §11.4 / §11.6 amendment #5.

**Files:**
- Modify: `orchestrator/services/cloud/nextcloud.py:1385-1404` (`seed_canary_fixture`/`remove_canary_fixture`), add `_enumerate_canary_refs`
- Modify (scope amendment, see below): `orchestrator/services/cloud/ro_probe.py` (`side_channel_probes` + `probe_read_only` gain `version_ref`/`trash_ref`; uploads-finalize probes a real reader-created upload session), `orchestrator/services/cloud/ro_engage.py` (true dav_root derivation + ref passthrough)
- Test: `tests/cloud/test_ro_canary_nextcloud.py` (create), `tests/cloud/test_ro_probe.py` + `tests/cloud/test_ro_engage.py` (extend)

**Scope amendment (added during execution — B1 review caught a plan omission):** curing the fixture alone leaves the refs dead code. Three wiring pieces are required for the stated purpose ("inconclusive → verified 403 on live NC") and belong to B1:
1. `side_channel_probes(dav_root, username, *, version_ref=None, trash_ref=None)` — when given, real refs replace `_SYNTHETIC_FILEID/_SYNTHETIC_VERSIONID` (versions-restore URL) and `_SYNTHETIC_TRASH_ITEM` (trash-restore URL). `probe_read_only` gains the same kwargs and passes them through.
2. `engage_ro_mount` currently passes `dav_root=grant.webdav_url` — the files-namespace URL, which builds nonsense side-channel URLs (`.../files/<reader>/<mount>/versions/...`). Derive the true DAV root (`{origin}/remote.php/dav`) from `grant.webdav_url` (split at `/remote.php/dav`) and pass `version_ref=canary.version_ref, trash_ref=canary.trash_ref`.
3. The uploads-finalize side channel can never be cured writer-side (the URL is reader-namespaced: `{dav}/uploads/{reader}/...`). Inside `probe_read_only`'s side-channel loop, before the uploads-finalize probe, MKCOL a real upload session `{dav_root}/uploads/{username}/srw-ro-probe` as the reader (MKCOL in one's own uploads namespace is a legitimate reader capability, not a folder write); on MKCOL 201, run the finalize MOVE against that real session (403/405 = rejected) and best-effort DELETE the session after; on MKCOL failure, fall back to the synthetic id (stays inconclusive, fail-closed). tus-create is unchanged (live status tuned at the manual validation gate).

**Interfaces:**
- Consumes: `CanaryFixture` (`base.py:382`), `self.put_project_folder_file_bytes`, `self.delete_project_folder_file`, `self._client` (httpx), `handle: ProjectFolderHandle`.
- Produces: `seed_canary_fixture(handle) -> CanaryFixture` now returns `version_ref`/`trash_ref` set to real ids when discoverable (still `None` when the server exposes none — those side channels stay inconclusive, fail-closed). The refs are `{fileid}/{versionid}` (versions) and `{trash_item_name}` (trashbin), the exact shapes `ro_probe.side_channel_probes` substitutes into its `_SYNTHETIC_*` slots.

- [ ] **Step 1: Write the failing test**

Create `tests/cloud/test_ro_canary_nextcloud.py`. Model the fake on `tests/cloud/test_ro_reader_nextcloud.py`'s transport-fake style — its binding helper is `_backend_with_ocs_fake()` (`test_ro_reader_nextcloud.py:165-174`): it sets `backend._client = httpx.AsyncClient(base_url=NC_BASE, transport=httpx.MockTransport(fake.handler))`, `backend._initialized = True`, `backend._agent_user = AGENT_USER`, `backend._agent_password = "pw"`. Copy that helper (and `_settings()`/`_handle()` at `:29`/`:40`) into this file as `_nc_backend_with_fake(fake)` — do NOT import private names across test modules. The fake must: accept the canary `PUT` (return 201 with an `OC-FileId` header), answer a `PROPFIND` on the versions namespace with one version href, and answer a `PROPFIND` on the trashbin with one trashed item.

```python
from __future__ import annotations

import httpx
import pytest

from orchestrator.services.cloud import NextcloudBackend, ProjectFolderHandle
# reuse the settings/handle/binding helpers already in test_ro_reader_nextcloud.py;
# if they are module-private, copy the minimal _settings()/_handle()/_bind(fake)
# shims into this file (do NOT import private names across test modules).


class FakeNcCanary:
    def __init__(self) -> None:
        self.put_path: str | None = None
        self.deleted: str | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        method, path = request.method, request.url.path
        if method == "PUT" and path.endswith("/.srw-ro-canary/probe.txt"):
            self.put_path = path
            return httpx.Response(201, headers={"OC-FileId": "12345"})
        if method == "PROPFIND" and "/versions/" in path:
            body = (
                '<?xml version="1.0"?><d:multistatus xmlns:d="DAV:">'
                "<d:response><d:href>/remote.php/dav/versions/agent-service/"
                "versions/12345/1699999999</d:href>"
                "<d:propstat><d:status>HTTP/1.1 200 OK</d:status></d:propstat>"
                "</d:response></d:multistatus>"
            )
            return httpx.Response(207, text=body)
        if method == "DELETE" and path.endswith("/.srw-ro-canary/probe.txt"):
            self.deleted = path
            return httpx.Response(204)
        if method == "PROPFIND" and "/trashbin/" in path:
            body = (
                '<?xml version="1.0"?><d:multistatus xmlns:d="DAV:">'
                "<d:response><d:href>/remote.php/dav/trashbin/agent-service/"
                "trash/probe.txt.d1699999999</d:href>"
                "<d:propstat><d:status>HTTP/1.1 200 OK</d:status></d:propstat>"
                "</d:response></d:multistatus>"
            )
            return httpx.Response(207, text=body)
        return httpx.Response(200, text="<d:multistatus xmlns:d='DAV:'/>")


@pytest.mark.asyncio
async def test_seed_canary_discovers_real_version_and_trash_refs():
    backend, fake = _nc_backend_with_fake(FakeNcCanary())  # local shim
    fixture = await backend.seed_canary_fixture(_handle())
    assert fixture.path == ".srw-ro-canary/probe.txt"
    assert fixture.version_ref == "12345/1699999999"
    assert fixture.trash_ref == "probe.txt.d1699999999"


@pytest.mark.asyncio
async def test_seed_canary_leaves_refs_none_when_server_exposes_none():
    class Empty(FakeNcCanary):
        def handler(self, request):
            if request.method == "PUT":
                return httpx.Response(201, headers={"OC-FileId": "12345"})
            if request.method == "PROPFIND":
                return httpx.Response(207, text="<d:multistatus xmlns:d='DAV:'/>")
            return httpx.Response(204)

    backend, _ = _nc_backend_with_fake(Empty())
    fixture = await backend.seed_canary_fixture(_handle())
    assert fixture.version_ref is None  # no version href -> stays inconclusive
    assert fixture.trash_ref is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/cloud/test_ro_canary_nextcloud.py -v`
Expected: FAIL — `seed_canary_fixture` currently hard-codes `version_ref=None, trash_ref=None`, so the first test's assertions fail.

- [ ] **Step 3: Implement the enumeration**

In `orchestrator/services/cloud/nextcloud.py`, replace `seed_canary_fixture` (currently `1385-1394`) and add the helper. Keep the existing `put_project_folder_file_bytes` seed; capture the returned `OC-FileId`, then PROPFIND the versions namespace for that fileid, and (after a delete-then-... no: the probe must not actually trash the canary before probing) — instead enumerate the **trashbin** only opportunistically: the canary is deleted at `remove_canary_fixture` time, so for the trash side channel we cannot rely on the canary being trashed during the probe window. Discover a real trash id by listing the reader/agent trashbin and taking any existing item; if the trashbin is empty, leave `trash_ref=None` (that side channel stays inconclusive, fail-closed — correct).

```python
    async def seed_canary_fixture(self, handle: ProjectFolderHandle) -> CanaryFixture:
        """Write a real canary file with the WRITE identity and discover REAL
        version/trash ids so the RO probe's CVE side channels target ids the
        server actually knows (design §11.4). A ref left None keeps that side
        channel inconclusive → the strict engage gate refuses (fail-closed)."""
        path = ".srw-ro-canary/probe.txt"
        fileid = await self._put_canary_and_get_fileid(handle, path)
        version_ref = await self._enumerate_version_ref(fileid) if fileid else None
        trash_ref = await self._enumerate_trash_ref()
        return CanaryFixture(path=path, version_ref=version_ref, trash_ref=trash_ref)

    async def _put_canary_and_get_fileid(
        self, handle: ProjectFolderHandle, path: str
    ) -> str | None:
        """PUT the canary (write identity) and return its numeric OC-FileId."""
        resp = await self._put_project_folder_file_bytes_raw(
            handle, path=path, content=b"canary"
        )
        fid = resp.headers.get("OC-FileId") if resp is not None else None
        if not fid:
            return None
        # OC-FileId is a 20-char padded token on some builds; the versions API
        # keys on the leading numeric id. Take the leading digits.
        digits = "".join(ch for ch in fid if ch.isdigit())
        return digits or None

    async def _enumerate_version_ref(self, fileid: str) -> str | None:
        """Return ``{fileid}/{versionid}`` for the newest version, or None.

        A brand-new single-write file may have zero versions; that's fine —
        None keeps the versions side channel inconclusive (fail-closed)."""
        user = self.webdav_credentials.get("username")
        if not user:
            return None
        url = f"/remote.php/dav/versions/{user}/versions/{fileid}"
        try:
            resp = await self._client.request("PROPFIND", url, headers={"Depth": "1"})
        except httpx.HTTPError:
            return None
        if resp.status_code != 207:
            return None
        version_id = self._first_child_leaf(resp.text, parent_fileid=fileid)
        return f"{fileid}/{version_id}" if version_id else None

    async def _enumerate_trash_ref(self) -> str | None:
        """Return a real trashed item name from the write identity's trashbin,
        or None when the trashbin is empty (side channel stays inconclusive)."""
        user = self.webdav_credentials.get("username")
        if not user:
            return None
        url = f"/remote.php/dav/trashbin/{user}/trash"
        try:
            resp = await self._client.request("PROPFIND", url, headers={"Depth": "1"})
        except httpx.HTTPError:
            return None
        if resp.status_code != 207:
            return None
        return self._first_child_leaf(resp.text, parent_fileid=None)
```

Add a small XML helper next to `parse_propfind_entries` usage (reuse `parse_propfind_entries` if its shape fits; otherwise a local minimal parser):

```python
    @staticmethod
    def _first_child_leaf(xml: str, *, parent_fileid: str | None) -> str | None:
        """Return the trailing path segment of the first <d:href> that is a
        strict child (not the collection self-href). Used to pull a real
        version id or trashed-item name out of a Depth:1 PROPFIND body."""
        import re

        hrefs = re.findall(r"<d:href>([^<]+)</d:href>", xml, flags=re.IGNORECASE)
        for href in hrefs:
            seg = href.rstrip("/").rsplit("/", 1)[-1]
            if not seg:
                continue
            if parent_fileid is not None and seg == parent_fileid:
                continue  # the collection self-href
            return seg
        return None
```

You also need `_put_project_folder_file_bytes_raw` — `put_project_folder_file_bytes` (`nextcloud.py:827-880`) returns `None`, so add a thin variant that performs the same MKCOL-parents-then-PUT against `self._groupfolder_dav_base(handle)` with `auth=(self._agent_user, self._agent_password)` but **returns the `put_resp`** so the caller can read `OC-FileId`. `self.webdav_credentials["username"]` == `self._agent_user` (`nextcloud.py:124-126`), so the version/trash namespaces are keyed to `self._agent_user` — use it directly.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/cloud/test_ro_canary_nextcloud.py -v`
Expected: PASS (both tests).

- [ ] **Step 5: Run the Slice A engage + probe suites for regression**

Run: `pytest tests/cloud/test_ro_engage.py tests/cloud/test_ro_reader_nextcloud.py -q`
Expected: PASS — `engage_ro_mount` still constructs and the NC reader trio is unaffected (the fixture now carries refs, which the existing `ro_engage` tests either ignore or already stub).

- [ ] **Step 6: Commit**

```bash
git add orchestrator/services/cloud/nextcloud.py tests/cloud/test_ro_canary_nextcloud.py
git commit -m "feat(cloud): cure NC canary fixture with real version/trash ids (RO probe live-ready)"
```

---

### Task B2: `OverlayMountManager` — mount/unmount the capture overlay

The agent-side component that, after the RO rclone lower is up at `/cloud/lower`, mounts fuse-overlayfs (`upperdir` in snapshot scope, `merged` outside it) and re-points `workspace/cloud → /cloud/merged`. Mirrors `RcloneMountManager`'s architecture exactly: pure Python that generates a bash script and runs it via `workspace_backend.exec_command`/`write_home_file`, with an `_OK` sentinel. Design §3.1, §11.3, §11.6 amendment #2 & #6.

**Files:**
- Create: `src/services/cloud_overlay/overlay_mount.py`
- Modify: `src/services/cloud_overlay/__init__.py` (currently 0 bytes — export the manager)
- Test: `tests/cloud_overlay/test_overlay_mount.py` (create; the `tests/cloud_overlay/` package already exists with `__init__.py`)

**Interfaces:**
- Consumes: `workspace_backend` with `exec_command(cmd, timeout) -> str`, `write_home_file(rel, content)`, `resolve_home_path(rel) -> str`, attribute `root` (same contract `RcloneMountManager` requires — `cloud_mount/__init__.py:406-411`). An `overlay_cfg` dict: `{"lower": "/cloud/lower", "upper": "/home/agent-host/.overlay/upper", "work": "/home/agent-host/.overlay/work", "merged": "/cloud/merged", "quota_bytes": 8589934592}`.
- Produces: `OverlayMountManager(thread_id, overlay_cfg, workspace_backend, workspace_root)` with `mount() -> None` (raises `OverlayMountError` on failure), `unmount() -> None` (idempotent, leaves upper/work intact), `active: bool`, and the internal `_mount_script()`/`_unmount_script()` used by later tasks. `_OVERLAY_OK = "__SRW_OVERLAY_OK__"`, `_OVERLAY_FAILED = "__SRW_OVERLAY_FAILED__"`.

- [ ] **Step 1: Write the failing test**

Create `tests/cloud_overlay/test_overlay_mount.py`. Reuse the `FakeRemoteBackend` shape from `tests/cloud_mount/test_rclone_mount_manager.py` (copy the minimal fake into this file — it's ~20 lines; do not import a private test helper across modules).

```python
from __future__ import annotations

from pathlib import Path

import pytest

from src.services.cloud_overlay.overlay_mount import (
    OverlayMountError,
    OverlayMountManager,
)


class FakeRemoteBackend:
    def __init__(self, *, root: str | None = None) -> None:
        self.files: dict[str, str] = {}
        self.commands: list[tuple[str, int]] = []
        self.outputs_by_script: dict[str, str] = {}
        if root is not None:
            self.root = root

    def resolve_home_path(self, relative_path: str) -> str:
        return f"/home/agent-host/{relative_path}"

    def write_home_file(self, relative_path: str, content) -> None:
        self.files[relative_path] = (
            content.decode("utf-8") if isinstance(content, bytes) else content
        )

    def exec_command(self, command: str, timeout: int = 30) -> str:
        self.commands.append((command, timeout))
        for name, out in self.outputs_by_script.items():
            if name in command:
                return out
        return "__SRW_OVERLAY_OK__\n"


def _cfg() -> dict:
    return {
        "lower": "/cloud/lower",
        "upper": "/home/agent-host/.overlay/upper",
        "work": "/home/agent-host/.overlay/work",
        "merged": "/cloud/merged",
        "quota_bytes": 8 * 1024**3,
    }


def _manager(backend) -> OverlayMountManager:
    return OverlayMountManager(
        thread_id="thread-12345678",
        overlay_cfg=_cfg(),
        workspace_backend=backend,
        workspace_root=Path("/home/agent-host/workspace"),
    )


def test_mount_script_builds_fuse_overlayfs_over_ro_lower_and_repoints_symlink():
    backend = FakeRemoteBackend()
    _manager(backend).mount()
    scripts = [b for p, b in backend.files.items() if p.endswith("overlay_mount.sh")]
    assert len(scripts) == 1
    s = scripts[0]
    # exact spike option string, no extra overlay opts (Global Constraints)
    assert (
        "fuse-overlayfs -o "
        "lowerdir=/cloud/lower,upperdir=/home/agent-host/.overlay/upper,"
        "workdir=/home/agent-host/.overlay/work /cloud/merged" in s
    )
    assert "mkdir -p /home/agent-host/.overlay/upper" in s
    assert "mountpoint -q /cloud/lower" in s  # refuses if the lower isn't up
    # symlink workspace/cloud -> merged (NOT the raw lower)
    assert "ln -sfn /cloud/merged" in s
    assert "${workspace}/cloud" in s


def test_mount_refuses_when_lower_not_mounted():
    backend = FakeRemoteBackend()
    backend.outputs_by_script["overlay_mount.sh"] = "__SRW_OVERLAY_FAILED__ rc=1\n"
    with pytest.raises(OverlayMountError):
        _manager(backend).mount()


def test_unmount_is_plain_and_leaves_upperdir(monkeypatch):
    backend = FakeRemoteBackend()
    mgr = _manager(backend)
    mgr.mount()
    mgr.unmount()
    scripts = [b for p, b in backend.files.items() if p.endswith("overlay_unmount.sh")]
    assert len(scripts) == 1
    u = scripts[0]
    assert "fusermount3 -u /cloud/merged" in u
    # unmount must NOT rm the upperdir/workdir
    assert "rm -rf /home/agent-host/.overlay/upper" not in u
    assert "rm -rf /home/agent-host/.overlay/work" not in u
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/cloud_overlay/test_overlay_mount.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.services.cloud_overlay.overlay_mount'`.

- [ ] **Step 3: Implement `OverlayMountManager`**

Create `src/services/cloud_overlay/overlay_mount.py`:

```python
"""fuse-overlayfs capture overlay over a read-only rclone lower.

Sibling to ``src.services.cloud_mount.RcloneMountManager``: pure Python that
generates a bash script and runs it on the WORKSPACE pod/VM over the workspace
backend's SSH channel (never the agent pod). The overlay stacks a local
scratch upperdir (the staged diff) over the read-only rclone mount so every
agent write — shell included — is captured locally and the cloud is untouched
until an operator applies the diff (design §3.1/§3.2).

Layout is fixed by the snapshot placement rule (design §11.3): upperdir/workdir
INSIDE /home/agent-host (captured by snapshots), merged mountpoint + raw rclone
lower OUTSIDE it (not captured). The agent sees the merged view at
``workspace/cloud`` via a symlink this manager owns.
"""

from __future__ import annotations

import logging
import shlex
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_OVERLAY_OK = "__SRW_OVERLAY_OK__"
_OVERLAY_FAILED = "__SRW_OVERLAY_FAILED__"


class OverlayMountError(RuntimeError):
    """The capture overlay could not be mounted/refreshed/healed."""


class OverlayMountManager:
    def __init__(
        self,
        *,
        thread_id: str,
        overlay_cfg: dict[str, Any],
        workspace_backend: Any,
        workspace_root: Path | str,
    ) -> None:
        self.thread_id = thread_id
        self.cfg = dict(overlay_cfg)
        self.workspace_backend = workspace_backend
        self.workspace_root = str(
            getattr(workspace_backend, "root", None) or workspace_root
        )
        self._active = False

    @property
    def active(self) -> bool:
        return self._active

    @property
    def lower(self) -> str:
        return str(self.cfg["lower"])

    @property
    def merged(self) -> str:
        return str(self.cfg["merged"])

    @property
    def upper(self) -> str:
        return str(self.cfg["upper"])

    @property
    def work(self) -> str:
        return str(self.cfg["work"])

    def mount(self) -> None:
        self._run("overlay_mount.sh", self._mount_script(), timeout=60)
        self._active = True
        logger.info("capture overlay mounted: thread=%s merged=%s", self.thread_id, self.merged)

    def unmount(self) -> None:
        try:
            self._run("overlay_unmount.sh", self._unmount_script(), timeout=45, require_ok=False)
        finally:
            self._active = False

    # --------------------------------------------------------------- scripts

    def _mount_script(self) -> str:
        lower = shlex.quote(self.lower)
        upper = shlex.quote(self.upper)
        work = shlex.quote(self.work)
        merged = shlex.quote(self.merged)
        workspace = shlex.quote(self.workspace_root)
        # Quote the WHOLE -o value: raw interpolation would word-split on paths
        # with spaces/metacharacters (B2 review finding).
        opts = shlex.quote(
            f"lowerdir={self.lower},upperdir={self.upper},workdir={self.work}"
        )
        return f"""#!/usr/bin/env bash
set -euo pipefail
umask 077
trap 'rc=$?; echo "{_OVERLAY_FAILED} rc=${{rc}}"; exit "${{rc}}"' ERR

# The RO rclone lower MUST already be mounted; refuse otherwise (fail-closed).
if ! mountpoint -q {lower}; then
  echo "{_OVERLAY_FAILED} rc=2 (lower {self.lower} not mounted)"
  exit 2
fi

mkdir -p {upper} {work} {merged}

# Re-mount idempotently: tear a stale overlay down first (plain, then lazy).
if mountpoint -q {merged}; then
  fusermount3 -u {merged} 2>/dev/null || fusermount -u {merged} 2>/dev/null || true
fi
if mountpoint -q {merged}; then
  fusermount3 -uz {merged} 2>/dev/null || fusermount -uz {merged} 2>/dev/null || true
fi

fuse-overlayfs -o {opts} {merged}

if ! mountpoint -q {merged}; then
  echo "{_OVERLAY_FAILED} rc=3 (overlay did not mount)"
  exit 3
fi

# Point the agent's workspace/cloud at the MERGED view (not the raw lower).
workspace={workspace}
mkdir -p "${{workspace}}/.srw"
entry="${{workspace}}/cloud"
if [ -L "${{entry}}" ]; then rm "${{entry}}"; fi
if [ -e "${{entry}}" ] && [ ! -L "${{entry}}" ]; then
  mv "${{entry}}" "${{workspace}}/.srw/cloud.pre-overlay.$(date +%s)"
fi
ln -sfn {merged} "${{entry}}"

echo "{_OVERLAY_OK}"
"""

    def _unmount_script(self) -> str:
        merged = shlex.quote(self.merged)
        return f"""#!/usr/bin/env bash
set +e
if mountpoint -q {merged}; then
  fusermount3 -u {merged} 2>/dev/null || fusermount -u {merged} 2>/dev/null
fi
if mountpoint -q {merged}; then
  fusermount3 -uz {merged} 2>/dev/null || fusermount -uz {merged} 2>/dev/null
fi
echo "{_OVERLAY_OK}"
"""

    # ----------------------------------------------------------- remote exec

    def _run(self, name: str, script: str, *, timeout: int = 30, require_ok: bool = True) -> str:
        rel = f".cache/srw/overlay/{self.thread_id}/scripts/{name}"
        self.workspace_backend.write_home_file(rel, script)
        script_path = self.workspace_backend.resolve_home_path(rel)
        command = (
            f"chmod 700 {shlex.quote(script_path)} && bash {shlex.quote(script_path)}; "
            f"rc=$?; rm -f {shlex.quote(script_path)}; exit $rc"
        )
        output = self.workspace_backend.exec_command(command, timeout=timeout)
        if require_ok and _OVERLAY_OK not in output:
            raise OverlayMountError(f"{name} did not report OK:\n{output}")
        return output
```

Set `src/services/cloud_overlay/__init__.py` to:

```python
from .overlay_mount import OverlayMountError, OverlayMountManager

__all__ = ["OverlayMountError", "OverlayMountManager"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/cloud_overlay/test_overlay_mount.py -v`
Expected: PASS (all three tests).

- [ ] **Step 5: Verify the whiteout enumerator still imports (no circular import)**

Run: `pytest tests/cloud_overlay/ -q`
Expected: PASS — `whiteout.py` (Slice 0) and the new `overlay_mount.py` coexist under the same package.

- [ ] **Step 6: Commit**

```bash
git add src/services/cloud_overlay/__init__.py src/services/cloud_overlay/overlay_mount.py tests/cloud_overlay/test_overlay_mount.py
git commit -m "feat(cloud): OverlayMountManager — fuse-overlayfs capture overlay over RO lower"
```

---

### Task B3: `vfs/refresh` + the overlay refresh op

The first-class refresh (design §3.4/§11.2): quiesce → plain-unmount overlay → `rclone rc vfs/refresh` on the lower → remount overlay. The rclone rc credentials live on `RcloneMountManager`'s `RcloneMountState`, so `RcloneMountManager` grows a `refresh_vfs()` method and `OverlayMountManager.refresh()` orchestrates the sequence, calling back into it.

**Files:**
- Modify: `src/services/cloud_mount/__init__.py` — add `refresh_vfs` (near `status`, ~:188)
- Modify: `src/services/cloud_overlay/overlay_mount.py` — add `refresh(refresh_lower)` + `_plain_unmount_script` + `_mount_body_only_script`
- Test: `tests/cloud_mount/test_rclone_mount_manager.py` (extend), `tests/cloud_overlay/test_overlay_mount.py` (extend)

**Interfaces:**
- Consumes: `RcloneMountState.rc_addr/rc_user/rc_pass/target_path/mount_id` (`cloud_mount/__init__.py:116-138`), `_run_remote_script` (`:858`), the `_OK` sentinel.
- Produces: `RcloneMountManager.refresh_vfs(mount_id: str | None = None, *, recursive: bool = True) -> None` — issues `rclone rc vfs/refresh recursive=true` against the mount's loopback rc; raises `RcloneMountError` on non-OK. `OverlayMountManager.refresh(refresh_lower: Callable[[], None]) -> None` — plain-unmount the overlay, call `refresh_lower()`, remount the overlay; the caller must have quiesced the agent (design §11.2: held FDs get silent stale reads; the plain `-u` EBUSYs if they didn't, which surfaces as `OverlayMountError`).

- [ ] **Step 1: Write the failing test (RcloneMountManager.refresh_vfs)**

In `tests/cloud_mount/test_rclone_mount_manager.py`, add (the fake returns `_OK` by default):

```python
def test_refresh_vfs_issues_vfs_refresh_not_forget():
    backend = FakeRemoteBackend()
    manager = RcloneMountManager(
        thread_id="thread-12345678",
        cloud_cfg=_cloud_mount_cfg(),
        workspace_backend=backend,
        workspace_root=Path("/home/agent-host/workspace"),
    )
    manager._start_all_sync()
    manager.refresh_vfs()
    scripts = [b for p, b in backend.files.items() if "vfs_refresh" in p]
    assert scripts, "expected a vfs_refresh script"
    s = scripts[0]
    assert "vfs/refresh" in s
    assert "recursive=true" in s
    assert "vfs/forget" not in s  # forget does NOT flush file content (design §11.2)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/cloud_mount/test_rclone_mount_manager.py::test_refresh_vfs_issues_vfs_refresh_not_forget -v`
Expected: FAIL — `AttributeError: 'RcloneMountManager' object has no attribute 'refresh_vfs'`.

- [ ] **Step 3: Implement `refresh_vfs`**

In `src/services/cloud_mount/__init__.py`, add after `status` (~:220):

```python
    def refresh_vfs(self, mount_id: str | None = None, *, recursive: bool = True) -> None:
        """Flush the rclone VFS so a subsequent read sees the live cloud.

        Uses ``vfs/refresh`` (re-PROPFINDs, invalidates changed content) — NOT
        ``vfs/forget``, which does not flush already-read file content (design
        §11.2). Applies to every active mount when ``mount_id`` is None.
        """
        targets = [
            s for s in self._states if mount_id is None or s.mount_id == mount_id
        ]
        for state in targets:
            self._run_remote_script(
                f"vfs_refresh_{state.remote_name}.sh",
                self._vfs_refresh_script(state, recursive=recursive),
                timeout=120,
            )

    def _vfs_refresh_script(self, state: RcloneMountState, *, recursive: bool) -> str:
        rec = "true" if recursive else "false"
        return f"""#!/usr/bin/env bash
set +e
rclone rc --rc-addr {shlex.quote(state.rc_addr)} --rc-user {shlex.quote(state.rc_user)} --rc-pass {shlex.quote(state.rc_pass)} vfs/refresh recursive={rec} >/dev/null 2>&1
rc=$?
if [ "$rc" -ne 0 ]; then
  echo "vfs/refresh failed rc=$rc" >&2
  exit "$rc"
fi
echo "{_OK}"
"""
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/cloud_mount/test_rclone_mount_manager.py::test_refresh_vfs_issues_vfs_refresh_not_forget -v`
Expected: PASS.

- [ ] **Step 5: Write the failing test (OverlayMountManager.refresh)**

In `tests/cloud_overlay/test_overlay_mount.py`:

```python
def test_refresh_unmounts_overlay_refreshes_lower_then_remounts():
    backend = FakeRemoteBackend()
    mgr = _manager(backend)
    mgr.mount()
    calls: list[str] = []
    mgr.refresh(lambda: calls.append("lower-refreshed"))
    assert calls == ["lower-refreshed"]
    # a remount script ran after the refresh callback
    scripts = [p for p in backend.files if p.endswith("overlay_remount.sh")]
    assert scripts, "expected an overlay_remount script"
    remount = next(b for p, b in backend.files.items() if p.endswith("overlay_remount.sh"))
    assert "fusermount3 -u /cloud/merged" in remount  # PLAIN unmount (not -uz)
    assert "fuse-overlayfs -o lowerdir=/cloud/lower" in remount
```

- [ ] **Step 6: Implement `refresh`**

The lower refresh must run **between** the overlay unmount and remount, so `refresh` is a three-step sequence (unmount overlay → refresh lower via callback → remount overlay), not one script. Add to `src/services/cloud_overlay/overlay_mount.py`:

```python
    def refresh(self, refresh_lower) -> None:
        """Refresh the frozen lower without losing the upperdir (design §11.2).

        CALLER MUST QUIESCE FIRST: held FDs across an unmount get silent stale
        reads. Sequence: PLAIN-unmount the overlay (``fusermount3 -u`` EBUSYs
        while an FD is held, so an un-quiesced agent surfaces as an
        OverlayMountError instead of silent staleness — never ``-uz`` here) →
        refresh the rclone lower (callback) → remount the overlay. The upperdir
        is untouched throughout; the workspace/cloud symlink already points at
        the merged path from the initial mount, so no symlink work is needed.
        """
        self._run("overlay_pre_refresh_unmount.sh", self._plain_unmount_script(), timeout=60)
        refresh_lower()
        self._run("overlay_remount.sh", self._mount_body_only_script(), timeout=120)

    def _plain_unmount_script(self) -> str:
        merged = shlex.quote(self.merged)
        return f"""#!/usr/bin/env bash
set -euo pipefail
trap 'rc=$?; echo "{_OVERLAY_FAILED} rc=${{rc}}"; exit "${{rc}}"' ERR
# PLAIN unmount — EBUSYs while an FD is held (the quiesce guard, design §11.2).
fusermount3 -u {merged} || fusermount -u {merged}
echo "{_OVERLAY_OK}"
"""

    def _mount_body_only_script(self) -> str:
        """Remount the overlay over the (refreshed) lower; no symlink work —
        the symlink already points at the merged path from the initial mount."""
        upper = shlex.quote(self.upper)
        work = shlex.quote(self.work)
        merged = shlex.quote(self.merged)
        lower = shlex.quote(self.lower)
        opts = shlex.quote(
            f"lowerdir={self.lower},upperdir={self.upper},workdir={self.work}"
        )
        return f"""#!/usr/bin/env bash
set -euo pipefail
trap 'rc=$?; echo "{_OVERLAY_FAILED} rc=${{rc}}"; exit "${{rc}}"' ERR
mkdir -p {upper} {work} {merged}
if ! mountpoint -q {lower}; then echo "{_OVERLAY_FAILED} rc=2 (lower not mounted)"; exit 2; fi
fuse-overlayfs -o {opts} {merged}
mountpoint -q {merged}
echo "{_OVERLAY_OK}"
"""
```

The Step-5 test asserts on the `overlay_remount.sh` script (the name `_mount_body_only_script` is written under), which matches the `_run("overlay_remount.sh", self._mount_body_only_script(), ...)` call above.

- [ ] **Step 7: Run tests to verify they pass**

Run: `pytest tests/cloud_overlay/test_overlay_mount.py tests/cloud_mount/test_rclone_mount_manager.py -q`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/services/cloud_mount/__init__.py src/services/cloud_overlay/overlay_mount.py tests/cloud_mount/test_rclone_mount_manager.py tests/cloud_overlay/test_overlay_mount.py
git commit -m "feat(cloud): overlay refresh op (quiesce + vfs/refresh, never vfs/forget)"
```

---

### Task B4: ENOTCONN health monitor + heal

`/proc/mounts` and `mountpoint -q` both lie over a dead rclone endpoint (design §11.2); the only reliable death signal is a read/readdir probe returning ENOTCONN. Heal = overlay `-uz` FIRST (safe here — the dead lower makes held-FD reads fail loudly), then remount the lower, then remount the overlay. This task adds a one-shot `health_check()` (probe → bool) and `heal(remount_lower)`; the periodic loop that calls them is minimal and mocked in tests.

**Files:**
- Modify: `src/services/cloud_overlay/overlay_mount.py` — `health_check`, `heal`, `_probe_script`, `_heal_unmount_script`
- Test: `tests/cloud_overlay/test_overlay_mount.py` (extend)

**Interfaces:**
- Consumes: the overlay `merged`/`lower` paths; `refresh_lower`-style callback for remounting the dead rclone lower (`remount_lower: Callable[[], None]`).
- Produces: `OverlayMountManager.health_check() -> bool` — runs a readdir probe on the merged view; returns `False` when the script reports ENOTCONN, `True` on `_OVERLAY_OK`. `OverlayMountManager.heal(remount_lower: Callable[[], None]) -> None` — lazy-unmount overlay → `remount_lower()` → remount overlay.

- [ ] **Step 1: Write the failing test**

```python
def test_health_check_true_on_ok_false_on_enotconn():
    ok_backend = FakeRemoteBackend()
    assert _manager(ok_backend).health_check() is True

    dead = FakeRemoteBackend()
    dead.outputs_by_script["overlay_probe.sh"] = "__SRW_OVERLAY_DEAD__ ENOTCONN\n"
    assert _manager(dead).health_check() is False


def test_heal_lazy_unmounts_overlay_first_then_remounts_lower_then_overlay():
    backend = FakeRemoteBackend()
    mgr = _manager(backend)
    order: list[str] = []
    mgr.heal(lambda: order.append("lower-remounted"))
    assert order == ["lower-remounted"]
    unmount = next(b for p, b in backend.files.items() if p.endswith("overlay_heal_unmount.sh"))
    assert "fusermount3 -uz /cloud/merged" in unmount  # LAZY is correct on heal
    remount = next(b for p, b in backend.files.items() if p.endswith("overlay_remount.sh"))
    assert "fuse-overlayfs -o lowerdir=/cloud/lower" in remount
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/cloud_overlay/test_overlay_mount.py::test_health_check_true_on_ok_false_on_enotconn -v`
Expected: FAIL — `AttributeError: ... has no attribute 'health_check'`.

- [ ] **Step 3: Implement `health_check` + `heal`**

Add to `OverlayMountManager` (note the new `_OVERLAY_DEAD` sentinel constant at module top: `_OVERLAY_DEAD = "__SRW_OVERLAY_DEAD__"`):

```python
    def health_check(self) -> bool:
        """True when the merged view is readable; False on ENOTCONN (dead lower).

        A readdir over the merged view is the only reliable liveness signal —
        /proc/mounts and ``mountpoint -q`` both report "mounted" over a dead
        rclone endpoint (design §11.2)."""
        out = self._run("overlay_probe.sh", self._probe_script(), timeout=30, require_ok=False)
        if _OVERLAY_DEAD in out:
            return False
        return _OVERLAY_OK in out

    def heal(self, remount_lower) -> None:
        """Recover a dead rclone lower under a live overlay (design §11.2).

        Lazy-unmount the overlay FIRST (safe here: the dead lower makes every
        held-FD read fail loudly with ENOTCONN — no silent-staleness window),
        remount the lower via the callback, then remount the overlay."""
        self._run("overlay_heal_unmount.sh", self._heal_unmount_script(), timeout=60, require_ok=False)
        remount_lower()
        self._run("overlay_remount.sh", self._mount_body_only_script(), timeout=120)

    def _probe_script(self) -> str:
        merged = shlex.quote(self.merged)
        return f"""#!/usr/bin/env bash
set +e
# A readdir returning ENOTCONN means the rclone lower died under us.
ls {merged} >/tmp/.srw-overlay-probe 2>/tmp/.srw-overlay-probe.err
rc=$?
if grep -qi 'not connected\\|ENOTCONN\\|Transport endpoint' /tmp/.srw-overlay-probe.err 2>/dev/null; then
  echo "{_OVERLAY_DEAD} ENOTCONN"
  exit 0
fi
if [ "$rc" -ne 0 ]; then echo "{_OVERLAY_DEAD} rc=$rc"; exit 0; fi
echo "{_OVERLAY_OK}"
"""

    def _heal_unmount_script(self) -> str:
        merged = shlex.quote(self.merged)
        return f"""#!/usr/bin/env bash
set +e
# LAZY unmount is correct on heal (dead lower => held reads ENOTCONN loudly).
fusermount3 -uz {merged} 2>/dev/null || fusermount -uz {merged} 2>/dev/null
echo "{_OVERLAY_OK}"
"""
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/cloud_overlay/test_overlay_mount.py -q`
Expected: PASS (health + heal + earlier tests).

- [ ] **Step 5: Commit**

```bash
git add src/services/cloud_overlay/overlay_mount.py tests/cloud_overlay/test_overlay_mount.py
git commit -m "feat(cloud): overlay ENOTCONN health-check + heal (overlay-first lazy unmount)"
```

---

### Task B5: Bulk-delete cost guard

`rm -rf` over the overlay is O(files) cold backend round-trips and can download full file bodies to whiteout them (design §11.1, §11.6 amendment #1). The existing `detect_cloud_scan_risk` flags recursive *reads* (grep/rg/du/cp) but not deletes. Add a delete-risk detector and wire it into the shell preflight so a broad delete over a cloud mount is warned/blocked like a broad scan.

**Files:**
- Modify: `src/services/cloud_mount/guardrails.py` — add `detect_cloud_delete_risk` + `format_cloud_delete_guard_message`
- Modify: `src/tools/shell/shell_tools.py` — call it in the same preflight as `_cloud_scan_guard_decision`
- Test: `tests/cloud_mount/test_cloud_scan_guard.py` (extend)

**Interfaces:**
- Consumes: `_argv_touches_cloud`, `_base_command`, `_has_recursive_flag`, `_contains_any`, `CloudScanRisk` (all in `guardrails.py`).
- Produces: `detect_cloud_delete_risk(command: str) -> CloudScanRisk | None` — flags `rm -r`/`rm -rf`/`rm` of a cloud path, and `find <cloudpath> ... -delete`. `format_cloud_delete_guard_message(command, risk) -> str`. The shell guard reuses the existing `scan_guard` mode (`block`/`warn`/`off`) from `cloud_mount_cfg`.

- [ ] **Step 1: Write the failing test**

In `tests/cloud_mount/test_cloud_scan_guard.py`:

```python
from src.services.cloud_mount.guardrails import detect_cloud_delete_risk


def test_detects_rm_rf_over_cloud_mount():
    assert detect_cloud_delete_risk("rm -rf /workspace/cloud/archive") is not None
    assert detect_cloud_delete_risk("rm -r /cloud/merged/old") is not None


def test_detects_find_delete_over_cloud_mount():
    assert (
        detect_cloud_delete_risk("find /workspace/cloud -name '*.tmp' -delete")
        is not None
    )


def test_ignores_deletes_outside_cloud_and_single_file_rm_elsewhere():
    assert detect_cloud_delete_risk("rm -rf /home/agent-host/workspace/build") is None
    assert detect_cloud_delete_risk("rm /workspace/cloud/one.txt") is None  # single file, no -r
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/cloud_mount/test_cloud_scan_guard.py -k delete -v`
Expected: FAIL — `ImportError: cannot import name 'detect_cloud_delete_risk'`.

- [ ] **Step 3: Implement the detector**

In `src/services/cloud_mount/guardrails.py`, add:

```python
def detect_cloud_delete_risk(command: str) -> CloudScanRisk | None:
    """Flag broad deletes over a cloud mount.

    Over a capture overlay, ``rm -rf`` is O(files) cold backend round-trips and
    can download full file bodies just to whiteout them (design §11.1). Catch
    recursive ``rm`` and ``find ... -delete`` pointed at a cloud path; a single
    ``rm file`` (no -r) is cheap enough to allow.
    """
    try:
        argv = shlex.split(command)
    except ValueError:
        if _mentions_cloud_path(command):
            return CloudScanRisk("delete command mentions a cloud mount but cannot be parsed safely")
        return None
    if not argv or not _argv_touches_cloud(argv):
        return None
    name = _base_command(argv)
    if name == "rm" and _has_recursive_flag(argv):
        return CloudScanRisk(
            "recursive rm over a cloud mount whiteouts each file (O(files) backend round-trips)"
        )
    if name == "find" and _contains_any(argv, {"-delete"}):
        return CloudScanRisk("find -delete over a cloud mount whiteouts matches one by one")
    return None


def format_cloud_delete_guard_message(
    command: str, risk: CloudScanRisk, *, protected: bool
) -> str:
    """The staged-semantics sentence is TRUE only under the capture overlay —
    on a live rw mount a delete is immediate and irreversible. The message must
    never claim safety the session doesn't have (B5 review finding)."""
    if protected:
        semantics = (
            "In protected mode a delete is STAGED (the cloud is untouched until "
            "you apply the diff), but whiteouting each file still costs a "
            "backend round-trip and may download its body first."
        )
    else:
        semantics = (
            "This session's cloud mount is LIVE: a delete removes the real "
            "cloud files immediately and is only recoverable via the cloud's "
            "own version history/trash, if any."
        )
    return (
        "Cloud delete guard: this command was not run because a broad delete "
        "over cloud storage is risky/expensive.\n"
        f"Reason: {risk.reason}.\n"
        f"Command: {command}\n\n"
        f"{semantics} Narrow the path, delete specific files, or confirm with "
        "the operator before a bulk delete."
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/cloud_mount/test_cloud_scan_guard.py -k delete -v`
Expected: PASS.

- [ ] **Step 5: Wire into the shell preflight**

In `src/tools/shell/shell_tools.py`, find where `_cloud_scan_guard_decision` is called in the shell tool preflight (grep `_cloud_scan_guard_decision(`). Add a sibling decision function and call it right after the scan decision, reusing the same `mode`:

```python
def _cloud_delete_guard_decision(
    command: str, context: ToolContext
) -> tuple[Optional[str], bool]:
    cloud_mount_cfg = context.get_config("cloud_mount", {})
    if not isinstance(cloud_mount_cfg, dict) or not cloud_mount_cfg.get("active"):
        return None, False
    mode = str(
        cloud_mount_cfg.get("scan_guard", os.getenv("SRW_CLOUD_SCAN_GUARD", "block"))
    ).lower()
    if mode in {"0", "off", "disabled", "false"}:
        return None, False
    from ...services.cloud_mount.guardrails import (
        detect_cloud_delete_risk,
        format_cloud_delete_guard_message,
    )

    risk = detect_cloud_delete_risk(command)
    if risk is None:
        return None, False
    message = format_cloud_delete_guard_message(
        command, risk, protected=bool(cloud_mount_cfg.get("protected"))
    )
    if mode == "warn":
        return (f"{message}\n\nThe command will still run because cloud_scan_guard=warn.", False)
    return message, True
```

Then at the call site (immediately after the existing scan-guard check), add:

```python
        delete_msg, delete_block = _cloud_delete_guard_decision(command, context)
        if delete_block:
            return delete_msg
        # (if warn) surface delete_msg the same way scan warn messages are surfaced
```

Match the exact return/prepend shape the surrounding scan-guard code uses (read those ~5 lines and mirror them; do not invent a new return convention).

- [ ] **Step 6: Run the shell-guard suite**

Run: `pytest tests/cloud_mount/test_cloud_scan_guard.py -q`
Expected: PASS (existing scan tests + new delete tests).

- [ ] **Step 7: Commit**

```bash
git add src/services/cloud_mount/guardrails.py src/tools/shell/shell_tools.py tests/cloud_mount/test_cloud_scan_guard.py
git commit -m "feat(cloud): bulk-delete guard for capture-overlay cloud mounts"
```

---

### Task B6: Upperdir quota + fail-at-cap guard

The upperdir shares the ~10Gi emptyDir; breaching an emptyDir `sizeLimit` **evicts the pod**, losing the session *and* the staged diff (design §7, §9.9 — chosen behavior: fail writes + warn). v1 enforcement is a soft cap: a `du`-based usage probe plus a shell-preflight guard that blocks new cloud writes when the upperdir is over its configured `quota_bytes` (hard ENOSPC via a loopback-backed upper is deferred and documented).

**Files:**
- Modify: `src/services/cloud_overlay/overlay_mount.py` — `upperdir_usage_bytes()`, `over_quota()`, `quota_guard_message()`
- Modify: `src/tools/shell/shell_tools.py` — consult the overlay quota in the preflight (like `_cloud_cache_guard_decision`)
- Modify (scope amendment — B6 review finding): `src/tools/workspace/files.py` — the file tools consume the cache guard via the path-based `_cloud_cache_guard_for_path` (:79-91) used by `read_file`/`write_file`/`edit_file`; add a WRITE-scoped sibling `_cloud_upperdir_guard_for_path(path, context)` calling `quota_guard_message()` and wire it into `write_file` + `edit_file` only (reads don't copy-up into the upper). Without this, file tools fill the upperdir past cap unguarded — the exact pod-eviction scenario §9.9 exists to prevent.
- Test: `tests/cloud_overlay/test_overlay_mount.py` (extend; include a `used == quota` boundary test), plus wiring tests for the file-tool guard wherever the cache-guard path tests live

**Interfaces:**
- Consumes: `self.cfg["quota_bytes"]`, `self.upper`, `_run` (require_ok=False), the `_OVERLAY_OK` sentinel.
- Produces: `OverlayMountManager.upperdir_usage_bytes() -> int` (parses `du -sb`), `over_quota() -> bool`, `quota_guard_message() -> str | None` (mirrors `RcloneMountManager.cache_limit_message`'s contract — returns a block message or None).

- [ ] **Step 1: Write the failing test**

```python
def test_upperdir_usage_parses_du_and_flags_over_quota():
    backend = FakeRemoteBackend()
    backend.outputs_by_script["overlay_usage.sh"] = "9663676416\t/home/agent-host/.overlay/upper\n__SRW_OVERLAY_OK__\n"
    mgr = _manager(backend)  # quota_bytes = 8 GiB
    assert mgr.upperdir_usage_bytes() == 9663676416
    assert mgr.over_quota() is True
    assert mgr.quota_guard_message() is not None


def test_under_quota_has_no_guard_message():
    backend = FakeRemoteBackend()
    backend.outputs_by_script["overlay_usage.sh"] = "1024\t/home/agent-host/.overlay/upper\n__SRW_OVERLAY_OK__\n"
    mgr = _manager(backend)
    assert mgr.over_quota() is False
    assert mgr.quota_guard_message() is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/cloud_overlay/test_overlay_mount.py -k quota -v`
Expected: FAIL — `AttributeError: ... has no attribute 'upperdir_usage_bytes'`.

- [ ] **Step 3: Implement the quota probe + guard**

```python
    def upperdir_usage_bytes(self) -> int:
        out = self._run("overlay_usage.sh", self._usage_script(), timeout=30, require_ok=False)
        for line in out.splitlines():
            line = line.strip()
            if not line or line.startswith("__SRW_"):
                continue
            head = line.split()[0]
            if head.isdigit():
                return int(head)
        return 0

    def over_quota(self) -> bool:
        quota = int(self.cfg.get("quota_bytes") or 0)
        return bool(quota) and self.upperdir_usage_bytes() >= quota

    def quota_guard_message(self) -> str | None:
        quota = int(self.cfg.get("quota_bytes") or 0)
        if not quota:
            return None
        used = self.upperdir_usage_bytes()
        if used < quota:
            return None
        return (
            "Cloud staging guard: this write was blocked because the protected-"
            "mode staging area (upperdir) has reached its cap.\n"
            f"Used {used} bytes of {quota} allowed.\n\n"
            "Staged changes are held locally until you review/apply them. Apply "
            "or reject the pending cloud diff to free space, or ask the operator "
            "to raise the cap."
        )

    def _usage_script(self) -> str:
        upper = shlex.quote(self.upper)
        return f"""#!/usr/bin/env bash
set +e
du -sb {upper} 2>/dev/null
echo "{_OVERLAY_OK}"
"""
```

- [ ] **Step 4: Wire into the shell preflight**

In `src/tools/shell/shell_tools.py`, the overlay manager is reachable the same way the rclone manager is. Extend `cloud_mount` tool config to also carry the overlay manager (this is set in `persistent_session.py` in Task B9; for now add the guard decision that reads `context.get_config("cloud_mount", {}).get("_overlay_manager")` and calls `quota_guard_message()`, guarded on it being non-None):

```python
def _cloud_upperdir_guard_decision(command: str, context: ToolContext) -> Optional[str]:
    cloud_mount_cfg = context.get_config("cloud_mount", {})
    if not isinstance(cloud_mount_cfg, dict) or not cloud_mount_cfg.get("active"):
        return None
    if not command_touches_cloud_mount(command):
        return None
    overlay = cloud_mount_cfg.get("_overlay_manager")
    if overlay is None or not hasattr(overlay, "quota_guard_message"):
        return None
    try:
        return overlay.quota_guard_message()
    except Exception as exc:
        logger.warning("Cloud upperdir guard check failed: %s", exc)
        return None
```

Call it alongside `_cloud_cache_guard_decision` in the preflight (mirror that call/return shape). A returned message blocks the command.

- [ ] **Step 5: Run tests**

Run: `pytest tests/cloud_overlay/test_overlay_mount.py -q && pytest tests/cloud_mount/test_cloud_scan_guard.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/services/cloud_overlay/overlay_mount.py src/tools/shell/shell_tools.py tests/cloud_overlay/test_overlay_mount.py
git commit -m "feat(cloud): upperdir quota probe + fail-at-cap shell guard"
```

---

### Task B7: Snapshot placement — `--xattrs`/`--acls` on capture + extract

Overlay whiteouts must round-trip through snapshots: char(0,0) whiteouts survive on an emptyDir even without xattrs, but opaque-dir xattrs and extraction onto some rootfs variants need them (design §11.3, §11.6 amendment #6). The merged mount is already outside snapshot scope (it lives at `/cloud/merged`, not under `/home/agent-host/`), and the upperdir at `/home/agent-host/.overlay/upper` is already inside scope — so **no include/exclude change is needed**; only the tar flags on both capture and extract.

**Files:**
- Modify: `orchestrator/services/snapshot_service.py:388-392` (the `tar -cf -` command)
- Modify: `orchestrator/services/ssh_helpers.py:28` (`EXTRACT_REMOTE_CMD`)
- Test: `tests/test_snapshot_ssh_extraction.py` (update pinned assertion), `tests/test_snapshot_work_marker.py` (add capture-flag assertion)

**Interfaces:**
- Consumes: nothing new.
- Produces: capture tar gains `--xattrs --xattrs-include='*' --acls`; `EXTRACT_REMOTE_CMD` becomes `"zstd -d | tar --xattrs --xattrs-include='*' --acls -xf - -C /"`.

- [ ] **Step 1: Update the pinned restore test first (it will fail)**

In `tests/test_snapshot_ssh_extraction.py`, find the assertion pinning `EXTRACT_REMOTE_CMD` (it asserts `argv[-1] == EXTRACT_REMOTE_CMD` and/or the exact string). Update the expected string to include the new flags:

```python
    assert EXTRACT_REMOTE_CMD == "zstd -d | tar --xattrs --xattrs-include='*' --acls -xf - -C /"
```

Run: `pytest tests/test_snapshot_ssh_extraction.py -q`
Expected: FAIL — the constant hasn't changed yet.

- [ ] **Step 2: Change `EXTRACT_REMOTE_CMD`**

In `orchestrator/services/ssh_helpers.py:28`:

```python
# Remote command run on the agent host to inflate + unpack a snapshot.
# --xattrs/--acls so fuse-overlayfs opaque-dir xattrs + whiteouts round-trip
# (protected cloud mode, design §11.3). char(0,0) whiteouts survive without
# them on emptyDir, but opaque markers and some rootfs variants need them.
EXTRACT_REMOTE_CMD = "zstd -d | tar --xattrs --xattrs-include='*' --acls -xf - -C /"
```

- [ ] **Step 3: Run the restore test to verify it passes**

Run: `pytest tests/test_snapshot_ssh_extraction.py -q`
Expected: PASS.

- [ ] **Step 4: Add a capture-flag assertion (failing) to the work-marker test**

In `tests/test_snapshot_work_marker.py`, the test patches `create_subprocess_exec`; capture the tar command it was called with and assert the flags. Find where the SSH argv is asserted (or add a capture) and add:

```python
    # the capture tar must request xattrs/acls so overlay whiteouts round-trip
    tar_cmd = _captured_ssh_tar_cmd  # however the test already reaches the argv
    assert "--xattrs" in tar_cmd
    assert "--acls" in tar_cmd
```

If the test doesn't currently expose the argv, assert on the recorded `create_subprocess_exec` call args (`mock_exec.call_args`), whose last positional is the `tar ... | zstd` string.

Run: `pytest tests/test_snapshot_work_marker.py -q`
Expected: FAIL — capture command has no `--xattrs` yet.

- [ ] **Step 5: Change the capture tar**

In `orchestrator/services/snapshot_service.py`, update the `tar_cmd` (currently `388-392`):

```python
            # Build SSH tar command. --xattrs/--acls so fuse-overlayfs opaque-dir
            # xattrs + whiteouts survive capture/restore (protected cloud mode,
            # design §11.3). The capture roots already EXCLUDE the merged overlay
            # mount (it lives at /cloud/merged, outside /home/agent-host) and
            # INCLUDE the upperdir at /home/agent-host/.overlay/upper.
            tar_cmd = (
                f"tar --xattrs --xattrs-include='*' --acls -cf - "
                f"{' '.join(exclude_patterns)} {' '.join(include_dirs)} 2>/dev/null"
                " | zstd -1 -T0"
            )
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/test_snapshot_work_marker.py tests/test_snapshot_ssh_extraction.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add orchestrator/services/snapshot_service.py orchestrator/services/ssh_helpers.py tests/test_snapshot_ssh_extraction.py tests/test_snapshot_work_marker.py
git commit -m "feat(snapshot): --xattrs/--acls so overlay whiteouts round-trip capture/restore"
```

---

### Task B8: Orchestrator engage wiring + RO+overlay `cloud_mount` payload

Tie the orchestrator side together: read a `protected_cloud` marker on the thread, call Slice A's `engage_ro_mount` once at create (fail-closed), and teach `_build_agent_cloud_mount` to emit an RO-lower + overlay `cloud_mount` payload from the persisted `cloud_ro_mounts` row. The user-facing checkbox that *sets* the marker is Slice C; here the marker is a plain `ThreadCreateRequest.protected_cloud` field (default False) so the mechanism is end-to-end and testable now.

**Files:**
- Modify: `orchestrator/main.py` — module-level import of `engage_ro_mount`/`RoEngageRefused` (near the other `orchestrator.services.cloud` imports, ~:180-190); `ThreadCreateRequest.protected_cloud` (~:17602); persist marker in `metadata_patch` (~:17757); `_engage_protected_cloud_for_thread` + call it after thread_mounts seed (~:17792); `_build_protected_cloud_mount` + protected branch in `_build_agent_cloud_mount` (~:18428)
- Test: `tests/test_thread_mount_rows.py` (extend), `tests/test_protected_cloud_engage_wiring.py` (create)

**Note (verified):** `httpx` and `ProjectFolderHandle` are already imported in `main.py` (`:263`, `:187`); `engage_ro_mount`/`RoEngageRefused` are NOT — add `from orchestrator.services.cloud.ro_engage import engage_ro_mount, RoEngageRefused` (match the existing cloud-import path style in the file). `postgres_db` has NO general metadata-merge method (only `merge_thread_workspace_context`), so `_record_protected_error` uses the inline `UPDATE threads SET metadata = ... || $2::jsonb` from `create_thread:17771-17780`.

**Interfaces:**
- Consumes: `engage_ro_mount` (`ro_engage.py:30`), `RoEngageRefused` (`ro_engage.py:26`), `postgres_db.get_ro_mount_by_thread` (`postgres.py:1282`), `main_cloud_router.for_backend`, `_is_protected_cloud_mode_enabled` (`main.py:1170`), `ProjectFolderHandle.from_db`, the `cloud_ro_mounts` row dict (fields `backend/reader_id/credentials/webdav_url/auth_kind/status`).
- Produces: `_build_protected_cloud_mount(row: dict, *, thread_id: str) -> dict | None` — the RO+overlay payload (below); a protected branch at the top of `_build_agent_cloud_mount` that returns it when `metadata.get("protected_cloud")` is set AND an active row exists; `_engage_protected_cloud_for_thread(thread_id, *, user_id, mount_rows, metadata) -> None` that calls `engage_ro_mount` and writes `metadata.protected_cloud_error` on refusal.

- [ ] **Step 1: Write the failing payload-builder test**

In `tests/test_thread_mount_rows.py` (imports `from main import ...`):

```python
from main import _build_protected_cloud_mount


def test_protected_cloud_mount_payload_is_ro_lower_plus_overlay():
    row = {
        "backend": "nextcloud",
        "reader_id": "srw-reader-abc",
        "credentials": "app-pass-xyz",
        "webdav_url": "https://nc.internal/remote.php/dav/files/srw-reader-abc/Proj/",
        "auth_kind": "basic",
        "status": "active",
    }
    payload = _build_protected_cloud_mount(row, thread_id="thread-1")
    assert payload["driver"] == "rclone"
    assert payload["protected"] is True
    # overlay layout obeys the snapshot placement rule (design §11.3)
    ov = payload["overlay"]
    assert ov["upper"].startswith("/home/agent-host/.overlay")
    assert ov["merged"] == "/cloud/merged"
    assert ov["lower"] == "/cloud/lower"
    # single RO lower mount, reader creds (NOT agent-service), read_only
    assert len(payload["mounts"]) == 1
    m = payload["mounts"][0]
    assert m["access"] == "read_only"
    assert m["target_path"] == "/cloud/lower"
    assert m["source"]["config"]["url"] == row["webdav_url"]
    assert m["source"]["config"]["user"] == "srw-reader-abc"
    assert m["auth"] == {"type": "basic", "password": "app-pass-xyz"}
    # tell the agent NOT to install workspace/cloud -> lower; the overlay owns it
    assert payload["skip_workspace_links"] is True


def test_protected_cloud_mount_none_for_inactive_or_non_nextcloud():
    assert _build_protected_cloud_mount({"status": "revoked", "backend": "nextcloud"}, thread_id="t") is None
    assert _build_protected_cloud_mount({"status": "active", "backend": "opencloud"}, thread_id="t") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_thread_mount_rows.py -k protected -v`
Expected: FAIL — `ImportError: cannot import name '_build_protected_cloud_mount'`.

- [ ] **Step 3: Implement the payload builder**

In `orchestrator/main.py`, near `_build_agent_cloud_mount` (~:18415), add. Note the `quota_bytes` default (8 GiB, below the 10Gi emptyDir cliff) and the NC cache block copied from `NextcloudBackend.build_rclone_mount_spec` (`nextcloud.py:226-235`):

```python
# Protected-mode overlay layout (design §11.3): upper/work INSIDE the snapshot
# scope (/home/agent-host), merged mount + raw rclone lower OUTSIDE it.
_PROTECTED_OVERLAY_UPPER = "/home/agent-host/.overlay/upper"
_PROTECTED_OVERLAY_WORK = "/home/agent-host/.overlay/work"
_PROTECTED_OVERLAY_MERGED = "/cloud/merged"
_PROTECTED_LOWER_TARGET = "/cloud/lower"
_PROTECTED_UPPERDIR_QUOTA_BYTES = 8 * 1024 * 1024 * 1024  # < 10Gi emptyDir cliff


def _build_protected_cloud_mount(
    row: dict[str, Any], *, thread_id: str
) -> Optional[dict[str, Any]]:
    """Build the RO-lower + capture-overlay cloud_mount payload from an active
    ``cloud_ro_mounts`` row (design §3.1). Nextcloud-only, read-only lower using
    the per-mount READER credential — never agent-service. Returns None when the
    row is not an active Nextcloud grant."""
    if not row or row.get("status") != "active" or row.get("backend") != "nextcloud":
        return None
    return {
        "version": 1,
        "driver": "rclone",
        "cloud_root": "/cloud",
        "workspace_entry": "cloud",
        "protected": True,
        # The overlay manager owns workspace/cloud -> merged; the rclone manager
        # must NOT install its own workspace/cloud -> lower symlink.
        "skip_workspace_links": True,
        "overlay": {
            "lower": _PROTECTED_LOWER_TARGET,
            "upper": _PROTECTED_OVERLAY_UPPER,
            "work": _PROTECTED_OVERLAY_WORK,
            "merged": _PROTECTED_OVERLAY_MERGED,
            "quota_bytes": _PROTECTED_UPPERDIR_QUOTA_BYTES,
        },
        "fallback": False,
        "mounts": [
            {
                "mount_id": f"protected-{thread_id}",
                "mount_kind": "protected_lower",
                "backend": "nextcloud",
                "target_path": _PROTECTED_LOWER_TARGET,
                "workspace_name": "lower",
                "access": "read_only",
                "source": {
                    "type": "webdav",
                    "config": {
                        "url": row["webdav_url"],
                        "vendor": "nextcloud",
                        "user": row["reader_id"],
                    },
                },
                "auth": {"type": "basic", "password": row["credentials"]},
                "cache": {
                    "vfs_cache_mode": "full",
                    "vfs_cache_max_size": "10G",
                    "vfs_cache_max_age": "24h",
                    "dir_cache_time": "5m",
                    "poll_interval": "1m",
                    "vfs_read_chunk_size": "16M",
                    "vfs_read_chunk_size_limit": "128M",
                    "hard_cache_limit": "20G",
                },
            }
        ],
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_thread_mount_rows.py -k protected -v`
Expected: PASS.

- [ ] **Step 5: Add the protected branch to `_build_agent_cloud_mount`**

At the top of `_build_agent_cloud_mount` (after the `_runtime_supports_rclone_mount` guard, ~:18429), add the protected short-circuit. It runs a read-only DB lookup — no engage here (engage is at create, Step 7):

```python
    # Protected cloud mode: the marker ALONE routes a thread into this branch —
    # never gate the branch on the feature flag, or a protected-marked thread
    # served while the flag is OFF would fall through to the LIVE builders with
    # agent-service credentials (B8 review finding; violates the fail-closed
    # invariant). Flag off ⇒ protected threads get NO cloud, not live cloud.
    if metadata.get("protected_cloud"):
        if not _is_protected_cloud_mode_enabled():
            logger.warning(
                "Thread %s: protected_cloud marker present but "
                "PROTECTED_CLOUD_MODE_ENABLED is off; refusing any cloud mount.",
                thread.get("id"),
            )
            return None
        vm_ctx = metadata.get("vm") or {}
        if vm_ctx.get("status") == "ready" and vm_ctx.get("ssh_host"):
            logger.warning(
                "Thread %s: protected cloud mode not supported on VM tier; no mount.",
                thread.get("id"),
            )
            return None
        row = await postgres_db.get_ro_mount_by_thread(str(thread.get("id")))
        return _build_protected_cloud_mount(row, thread_id=str(thread.get("id"))) if row else None
```

- [ ] **Step 5b: Fail-close the endpoint's cloud_sync fallback for protected threads (scope amendment — B8 review)**

In `agent_get_thread_workspace` (~:16612-16621), when the protected branch yields no mount (flag off, VM tier, refused/absent grant), the `elif _cloud_workspace_driver() == "rclone_mount"` fallback would hand the thread a LIVE session-folder `cloud_sync` — a live write path on a thread the user marked protected. Guard the fork:

```python
    if cloud_mount_cfg:
        cloud_sync_cfg = None
    elif metadata.get("protected_cloud"):
        # Protected thread with no engageable protected mount: NO live sync
        # fallback of any kind (fail-closed; agent sees degraded-cloud state).
        cloud_sync_cfg = None
    elif _cloud_workspace_driver() == "rclone_mount":
        ...
```

Test: extend the endpoint-level coverage (or the fork logic if extracted) so a `protected_cloud` thread with no mount payload gets `cloud_sync=None` (and the degraded flag set, since cloud is up but nothing resolved).

- [ ] **Step 6: Write the failing engage-wiring test**

Create `tests/test_protected_cloud_engage_wiring.py`. Patch `engage_ro_mount` and the flag; assert the marker path calls engage and that a refusal writes `protected_cloud_error` without raising:

```python
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

import main
from orchestrator.services.cloud.ro_engage import RoEngageRefused


@pytest.mark.asyncio
async def test_engage_called_for_protected_thread_with_project_mount():
    mount_rows = [{"backend_id": "nextcloud", "cloud_handle": "handle::Proj"}]
    with patch.object(main, "_is_protected_cloud_mode_enabled", return_value=True), \
         patch.object(main, "engage_ro_mount", new=AsyncMock()) as engage, \
         patch.object(main.main_cloud_router, "for_backend") as for_backend:
        for_backend.return_value = object()
        await main._engage_protected_cloud_for_thread(
            "thread-1", user_id="user-1", mount_rows=mount_rows, metadata={}
        )
    engage.assert_awaited_once()


@pytest.mark.asyncio
async def test_engage_refusal_records_error_and_does_not_raise():
    mount_rows = [{"backend_id": "nextcloud", "cloud_handle": "handle::Proj"}]
    recorded: list[str] = []
    with patch.object(main, "_is_protected_cloud_mode_enabled", return_value=True), \
         patch.object(main, "engage_ro_mount", new=AsyncMock(side_effect=RoEngageRefused("floor"))), \
         patch.object(main.main_cloud_router, "for_backend", return_value=object()), \
         patch.object(
             main, "_record_protected_error",
             new=AsyncMock(side_effect=lambda tid, msg: recorded.append(msg)),
         ):
        # must NOT raise — a refusal is recorded, the session boots with no mount
        await main._engage_protected_cloud_for_thread(
            "thread-1", user_id="user-1", mount_rows=mount_rows, metadata={}
        )
    assert recorded and "refused" in recorded[0]
```

(`_record_protected_error` writes the marker via the inline `UPDATE threads SET metadata = ... || $2::jsonb` pattern from `create_thread:17771-17780` — `postgres_db` has no general metadata-merge method. Patching `_record_protected_error` keeps this test off the DB.)

- [ ] **Step 7: Implement `_engage_protected_cloud_for_thread` + call it at create**

Add near `_build_agent_cloud_mount` in `orchestrator/main.py`:

```python
async def _engage_protected_cloud_for_thread(
    thread_id: str,
    *,
    user_id: str,
    mount_rows: list[dict[str, Any]] | None,
    metadata: dict[str, Any],
) -> None:
    """Engage protected cloud mode ONCE at thread create (design §3.3/§11.4).

    Picks the first Nextcloud-backed project mount, provisions the per-user
    reader + per-mount RO grant, and runs the fail-closed probe via
    ``engage_ro_mount`` — persisting a ``cloud_ro_mounts`` row on success. On
    refusal, records ``metadata.protected_cloud_error`` so the session boots
    with NO cloud mount (never a live one) and the agent can say why."""
    if not _is_protected_cloud_mode_enabled():
        return
    row = next(
        (
            r
            for r in (mount_rows or [])
            if r.get("backend_id") == "nextcloud" and r.get("cloud_handle")
        ),
        None,
    )
    if row is None:
        await _record_protected_error(thread_id, "no Nextcloud project mount to protect")
        return
    backend = main_cloud_router.for_backend("nextcloud")
    try:
        handle = ProjectFolderHandle.from_db(row["cloud_handle"], backend="nextcloud")

        def _reader_client(credentials: str | None):
            # httpx client authenticated AS THE READER (basic auth), for the probe.
            return httpx.AsyncClient(
                base_url=backend._base_url,  # NC origin
                auth=(f"srw-reader-{user_id}", credentials or ""),
                timeout=30.0,
            )

        await engage_ro_mount(
            backend=backend,
            handle=handle,
            user_key=user_id,
            thread_id=thread_id,
            user_id=user_id,
            postgres_db=postgres_db,
            http_client_factory=_reader_client,
        )
    except RoEngageRefused as e:
        await _record_protected_error(thread_id, f"protected mode refused: {e}")
    except Exception as e:  # provisioning error — fail closed, no mount
        logger.warning("Thread %s: protected engage failed: %s", thread_id, e)
        await _record_protected_error(thread_id, f"protected engage error: {e}")


async def _record_protected_error(thread_id: str, message: str) -> None:
    async with postgres_db.acquire() as conn:
        await conn.execute(
            "UPDATE threads SET metadata = COALESCE(metadata,'{}') || $2::jsonb WHERE id=$1",
            thread_id,
            json.dumps({"protected_cloud_error": message}),
        )
```

Add the `ThreadCreateRequest.protected_cloud` field (after `config_override`, ~:17602):

```python
    protected_cloud: bool = Field(
        False,
        description=(
            "Protected cloud mode: mount the project cloud folder read-only with "
            "a capture overlay so agent writes are staged for review, not live. "
            "Nextcloud-only, container-runtime-only (design §3, §9.2). The New "
            "Session checkbox that sets this lands in Slice C."
        ),
    )
```

Persist the marker in `metadata_patch` (after the `datasource_ids` block, ~:17769):

```python
        if request_body.protected_cloud:
            metadata_patch["protected_cloud"] = True
```

Call engage right after the thread_mounts seed (~:17791, inside the `if effective_project_ids:` success path or just after it). Do it as a fire-and-forget task so create latency is unaffected, mirroring `_provision_thread_workspace`:

```python
        if request_body.protected_cloud and _is_protected_cloud_mode_enabled():
            seeded_rows = await postgres_db.list_thread_mounts(thread_id)

            async def _engage_protected(tid: str) -> None:
                await _engage_protected_cloud_for_thread(
                    tid,
                    user_id=str(user["id"]),
                    mount_rows=seeded_rows,
                    metadata={},
                )

            asyncio.create_task(_engage_protected(thread_id))
```

(Confirm `httpx` is imported in `main.py`; grep `^import httpx`. If absent, add it.)

- [ ] **Step 8: Run the wiring + payload tests**

Run: `pytest tests/test_protected_cloud_engage_wiring.py tests/test_thread_mount_rows.py -q`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add orchestrator/main.py tests/test_thread_mount_rows.py tests/test_protected_cloud_engage_wiring.py
git commit -m "feat(cloud): engage protected mode at create + RO-lower/overlay cloud_mount payload"
```

---

### Task B9: Agent-side stack wiring — mount RO lower, then overlay

The final integration: `_setup_cloud_mount` mounts the RO rclone lower (unchanged `RcloneMountManager`, now honoring `skip_workspace_links`), then — for a protected payload — mounts the `OverlayMountManager` on top and exposes it to tools. Teardown unmounts overlay before lower.

**Files:**
- Modify: `src/services/cloud_mount/__init__.py` — `_start_all_sync` skips `_install_workspace_links` when `cloud_cfg.get("skip_workspace_links")`
- Modify: `src/api/persistent_session.py` — `_setup_cloud_mount` overlay stacking; teardown; tool-config `_overlay_manager`
- Test: `tests/cloud_mount/test_rclone_mount_manager.py` (extend), `tests/test_persistent_app.py` or a focused new test for the stacking

**Interfaces:**
- Consumes: `OverlayMountManager` (B2), `RcloneMountManager` (with `refresh_vfs` from B3), the protected payload from B8 (`cloud_cfg["protected"]`, `cloud_cfg["overlay"]`, `cloud_cfg["skip_workspace_links"]`).
- Produces: a protected session whose `workspace/cloud` is the merged overlay; `self.overlay_mount_manager` attribute; tool config `cloud_mount._overlay_manager` (consumed by B6's guard).

- [ ] **Step 1: Write the failing `skip_workspace_links` test**

In `tests/cloud_mount/test_rclone_mount_manager.py`:

```python
def test_skip_workspace_links_omits_symlink_install():
    cfg = _cloud_mount_cfg()
    cfg["skip_workspace_links"] = True
    backend = FakeRemoteBackend()
    manager = RcloneMountManager(
        thread_id="thread-12345678",
        cloud_cfg=cfg,
        workspace_backend=backend,
        workspace_root=Path("/home/agent-host/workspace"),
    )
    manager._start_all_sync()
    link_scripts = [p for p in backend.files if p.endswith("install_cloud_links.sh")]
    assert link_scripts == []  # overlay owns the symlink in protected mode
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/cloud_mount/test_rclone_mount_manager.py::test_skip_workspace_links_omits_symlink_install -v`
Expected: FAIL — the symlink script is still installed.

- [ ] **Step 3: Honor `skip_workspace_links`**

In `src/services/cloud_mount/__init__.py`, in `_start_all_sync` (~:428), guard the link install:

```python
        if not self.cloud_cfg.get("skip_workspace_links"):
            self._install_workspace_links(states)
        self._states = states
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/cloud_mount/test_rclone_mount_manager.py::test_skip_workspace_links_omits_symlink_install -v`
Expected: PASS.

- [ ] **Step 5: Stack the overlay in `_setup_cloud_mount`**

In `src/api/persistent_session.py`, extend `_setup_cloud_mount` (`478-503`). After `start_all()` succeeds, if the payload is protected, mount the overlay:

```python
    async def _setup_cloud_mount(
        self, cloud_mount_cfg: Optional[Dict[str, Any]]
    ) -> None:
        """Start the RO rclone lower, then (protected mode) the capture overlay."""
        if not cloud_mount_cfg or not self.workspace_manager:
            return
        try:
            from src.services.cloud_mount import RcloneMountManager

            self.cloud_mount_manager = RcloneMountManager(
                thread_id=self.thread_id,
                cloud_cfg=cloud_mount_cfg,
                workspace_backend=self.workspace_manager.backend,
                workspace_root=self.workspace_manager.path,
            )
            await self.cloud_mount_manager.start_all()
            logger.info(
                "Cloud mount manager started with %d mount(s)",
                len(self.cloud_mount_manager.mounts),
            )
        except Exception as e:
            self.cloud_mount_error = str(e)
            self.cloud_mount_manager = None
            logger.warning("Failed to start cloud mount manager: %s", e)
            return

        if cloud_mount_cfg.get("protected") and cloud_mount_cfg.get("overlay"):
            try:
                from src.services.cloud_overlay import OverlayMountManager

                self.overlay_mount_manager = OverlayMountManager(
                    thread_id=self.thread_id,
                    overlay_cfg=cloud_mount_cfg["overlay"],
                    workspace_backend=self.workspace_manager.backend,
                    workspace_root=self.workspace_manager.path,
                )
                # runs the mount script on the workspace pod over SSH
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, self.overlay_mount_manager.mount)
                logger.info("Capture overlay mounted for protected session %s", self.thread_id)
            except Exception as e:
                self.cloud_mount_error = f"overlay: {e}"
                self.overlay_mount_manager = None
                logger.warning("Failed to mount capture overlay: %s", e)
```

Initialize `self.overlay_mount_manager = None` where `self.cloud_mount_manager`/`self.cloud_mount_error` are initialized (`persistent_session.py:219-220`).

- [ ] **Step 6: Expose the overlay manager to tools + teardown**

In the tool-config `cloud_mount` dict (`persistent_session.py:654-662`), add (the `protected` flag drives the B5 delete-guard message honesty — live vs staged semantics):

```python
                "protected": bool(
                    self.overlay_mount_manager and self.overlay_mount_manager.active
                ),
                "_overlay_manager": self.overlay_mount_manager,
```

In the teardown path (`persistent_session.py:1282-1287`, where `aclose()` is called on the cloud mount manager), unmount the overlay FIRST:

```python
        if self.overlay_mount_manager is not None:
            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, self.overlay_mount_manager.unmount)
            except Exception:
                logger.debug("overlay unmount failed", exc_info=True)
        if self.cloud_mount_manager is not None:
            await self.cloud_mount_manager.aclose()
```

- [ ] **Step 7: Write a focused stacking test**

Add a test that a protected cfg drives both managers. Use the existing `test_persistent_app.py` fakes if they cover `_setup_cloud_mount`; otherwise a minimal test that constructs a `PersistentSession`-like shim is heavy — instead unit-test the decision by asserting the overlay manager is created when `protected` is set. If `_setup_cloud_mount` is hard to isolate, assert at the `RcloneMountManager`+`OverlayMountManager` seam with a fake workspace backend (both accept the same `FakeRemoteBackend`). Keep it small:

```python
def test_protected_payload_mounts_lower_then_overlay(monkeypatch):
    # RcloneMountManager mounts the RO lower with skip_workspace_links;
    # OverlayMountManager mounts the overlay and installs workspace/cloud -> merged.
    from src.services.cloud_mount import RcloneMountManager
    from src.services.cloud_overlay import OverlayMountManager
    backend = FakeRemoteBackend()
    cfg = {  # shape of _build_protected_cloud_mount output
        "driver": "rclone", "protected": True, "skip_workspace_links": True,
        "overlay": {"lower": "/cloud/lower", "upper": "/home/agent-host/.overlay/upper",
                    "work": "/home/agent-host/.overlay/work", "merged": "/cloud/merged",
                    "quota_bytes": 8 * 1024**3},
        "mounts": [{"mount_id": "protected-t", "mount_kind": "protected_lower",
                    "target_path": "/cloud/lower", "workspace_name": "lower",
                    "access": "read_only", "backend": "nextcloud",
                    "source": {"type": "webdav", "config": {"url": "https://nc/x/", "vendor": "nextcloud", "user": "srw-reader-u"}},
                    "auth": {"type": "basic", "password": "p"}}],
    }
    RcloneMountManager(thread_id="thread-t", cloud_cfg=cfg, workspace_backend=backend,
                       workspace_root=Path("/home/agent-host/workspace"))._start_all_sync()
    assert not any(p.endswith("install_cloud_links.sh") for p in backend.files)
    OverlayMountManager(thread_id="thread-t", overlay_cfg=cfg["overlay"], workspace_backend=backend,
                        workspace_root=Path("/home/agent-host/workspace")).mount()
    assert any(p.endswith("overlay_mount.sh") for p in backend.files)
```

Place it in `tests/cloud_overlay/test_overlay_mount.py` (it needs the `FakeRemoteBackend` already there).

- [ ] **Step 8: Run the full protected-mode test surface**

Run: `pytest tests/cloud_overlay/ tests/cloud_mount/ tests/cloud/test_ro_engage.py tests/cloud/test_ro_canary_nextcloud.py tests/test_thread_mount_rows.py tests/test_protected_cloud_engage_wiring.py tests/test_snapshot_ssh_extraction.py -q`
Expected: PASS.

- [ ] **Step 9: Run ruff + the broad cloud suite**

Run: `ruff check src/services/cloud_overlay src/services/cloud_mount orchestrator/services/cloud orchestrator/main.py`
Expected: clean.
Run: `pytest tests/cloud/ -q`
Expected: PASS.

- [ ] **Step 10: Commit**

```bash
git add src/services/cloud_mount/__init__.py src/api/persistent_session.py tests/cloud_mount/test_rclone_mount_manager.py tests/cloud_overlay/test_overlay_mount.py
git commit -m "feat(cloud): stack capture overlay on RO lower in protected sessions"
```

---

## Post-Slice-B manual validation (documented, not CI)

Live validation is a manual step on k3d against real Nextcloud (design §11.4, matching Slice A's method): tilt live-syncs the code; create a protected session against a Nextcloud whose **groupfolders app is ≥ 20.1.2** (dev is 19.1.18 — bump first, per the memory note), verify: (1) `engage_ro_mount` passes the cured canary probe and persists a `cloud_ro_mounts` row; (2) the workspace pod mounts the RO rclone lower at `/cloud/lower` and fuse-overlayfs at `/cloud/merged`; (3) `echo x > workspace/cloud/probe.txt` lands in `/home/agent-host/.overlay/upper`, NOT in the cloud; (4) a snapshot captures the upperdir with whiteouts intact and 0 cloud bytes; (5) refresh + heal behave per §11.2. This is the Slice B live-validation gate before Slice C builds the review/apply UI on top.

---

## Slice C — master outline (plan in full after Slice B lands)

Delivers the session-facing surface: the New Session **protected checkbox** (`ThreadCreateRequest.protected_cloud` already exists from B8 — Slice C adds the Cockpit control + persistence UX), the auto-stage-at-turn-end → S3 push, the `DiffSource`-generalized review surface (reusing `job-diff-review`), apply/reject over the upperdir diff (consuming `src/services/cloud_overlay/whiteout.enumerate_diff` with the added-dirs-as-opaque 404-tolerance, amendment #2), and the agent-honesty prompt copy. Anticipated tasks unchanged from the Slice A plan's outline (`docs/superpowers/plans/2026-07-09-...slice-a-cloud-plumbing.md:1501-1511`).

---

## Self-review notes (author)

- **Spec coverage (Slice B vs design §11.6 amendments):** amendment #1 bulk-delete cost → B5; #2 added-dirs-as-opaque → carried into Slice C apply (the enumerator contract is already in `whiteout.py`; B does not apply diffs); #3 refresh/quiesce + ENOTCONN → B3+B4; #4 infinity-first etag walk + `_propfind` fix → **landed in Slice A** (not re-done here); #5 RO-probe fixture cure + live validation → B1 (cure) + the manual-validation section (live); #6 snapshot placement + tar xattrs → B7. Slice B outline tasks 1–6 (Slice A plan §1494-1499) → B8 (RO lower from grant), B2 (overlay mount), B3+B4 (refresh/heal), B5 (bulk-delete), B7 (snapshot), B6 (quota).
- **Deliberate refinement of the Slice A outline:** the outline put "branch the provisioning fork to call `engage_ro_mount`" in Slice C. This plan moves the *mechanism* (engage call + RO payload) into Slice B (B8) so Slice B is an end-to-end-functional, live-validatable feature (an operator sets `protected_cloud=True` on the create request); Slice C keeps only the *user surface* (Cockpit checkbox, review/apply, honesty copy). This matches the "add the key to the seam now" pattern and Slice A's precedent of shipping mechanism ahead of UI.
- **Type consistency:** the overlay cfg dict keys (`lower/upper/work/merged/quota_bytes`) are identical in `_build_protected_cloud_mount` (B8), `OverlayMountManager` (B2), and the B9 stacking test. `_OVERLAY_OK`/`_OVERLAY_FAILED`/`_OVERLAY_DEAD` are defined once in B2 (module top; `_OVERLAY_DEAD` added in B4) and reused. `refresh_vfs` (B3) is the exact method `OverlayMountManager.refresh`'s callback invokes in B9. `skip_workspace_links` is written by B8, honored by B9.
- **Pinned against real code (verified while writing):** `RcloneMountState` carries `rc_addr/rc_user/rc_pass/target_path/mount_id` (`cloud_mount/__init__.py:116-138`); `_run_remote_script(name, script, *, timeout=30, require_ok=True)` and the `_OK` sentinel (`:858`, `:27`); `_install_workspace_links` is the single symlink installer called at `:428`; `engage_ro_mount(*, backend, handle, user_key, thread_id, user_id, postgres_db, http_client_factory)` (`ro_engage.py:30`); `get_ro_mount_by_thread` returns a decrypted-credentials dict (`postgres.py:1282`, `_ro_mount_row`); the snapshot tar at `snapshot_service.py:388-392` and `EXTRACT_REMOTE_CMD` at `ssh_helpers.py:28`; `_setup_cloud_mount` at `persistent_session.py:478` runs before shell/tools (`:292`); tool-config `cloud_mount` dict at `:654-662`. NC cache block copied verbatim from `nextcloud.py:226-235`.
- **Still verify-against-reality at implementation time:** (a) the transport-fake binding helper name in `test_ro_reader_nextcloud.py` (B1's `_nc_backend_with_fake` shim); (b) whether `put_project_folder_file_bytes` already returns the response (drop the `_raw` shim if so) and the exact NC versions/trashbin DAV URL shapes for the reader vs write identity; (c) the exact shell-preflight return convention in `shell_tools.py` for scan/cache guards (B5/B6 must mirror it); (d) the metadata-merge helper name in `postgres.py` (B8's `_record_protected_error` falls back to the inline `UPDATE ... || $2::jsonb` used in `create_thread`); (e) `httpx` import presence in `main.py` (B8).
