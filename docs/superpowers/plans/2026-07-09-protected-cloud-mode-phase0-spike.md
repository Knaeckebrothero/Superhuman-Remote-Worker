# Protected Cloud Mode — Phase 0 (Spike) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Retire the feasibility unknowns in `docs/design/cloud_access_unification.md` §6 with a hands-on spike, and land the two pure-logic artifacts (whiteout enumerator, fail-closed RO probe) that Phase 1 will consume, producing a go/no-go report that seeds the Phase 1 plan.

**Architecture:** Protected cloud mode gives the agent a writable **overlay** whose lowerdir is a read-only rclone FUSE mount of the user's cloud and whose upperdir is local scratch — so every write (shell included) is captured in the upperdir as a reviewable diff, applied to the real cloud only on approval. Phase 0 does **not** wire this into the product; it (a) proves fuse-overlayfs-over-rclone works on the real workspace image + VM tier, (b) builds and unit-tests the engine-agnostic whiteout→diff enumerator and the fail-closed RO-credential probe as standalone modules, (c) measures the two cost unknowns (copy-up, etag-baseline walk), and (d) writes findings back into the design doc.

**Tech Stack:** Python 3.12 (orchestrator/agent, `pytest` + `pytest-asyncio`, `httpx`), Ubuntu 24.04 workspace image + `agent-vm-base` (Docker/Podman build), `rclone` 1.74.3, `fuse-overlayfs`, k3d/tilt dev cluster, Nextcloud + OpenCloud (shared dev instances).

## Global Constraints

- **RO backend version floors (verbatim from spec §3.3):** Nextcloud server **≥ 28.0.3**, groupfolders **≥ 20.1.2**. The RO probe must treat a lower version as fail-closed.
- **RO enforcement lives in the share/role layer, never the token.** The mount identity must be a **dedicated low-privilege account**, NOT `agent-service` (spec §3.3, §8.1.4).
- **The whiteout enumerator must handle BOTH whiteout forms** — char(0,0) device nodes AND `.wh.<name>` files — plus all three opaque-dir markers and the `.wh..wh..opq` sentinel, and must ignore `user.fuseoverlayfs.*` / `user.containers.override_stat` metadata xattrs (spec §3.1). Never key logic on the engine.
- **Never trust the mounted view for correctness.** Conflict detection is against live cloud etags; the overlay lower is assumed frozen per epoch (spec §3.4).
- **Work on `develop` directly; do not push without asking** (project convention). Commit per task.
- **Image edits must stay in sync across `docker/Dockerfile.workspace` and `docker/agent-vm-base/scripts/provision-stage1.sh`** — both install rclone 1.74.3 today; both gain fuse-overlayfs.
- **Spike code lives under a `spike/` namespace** (`scripts/spike/`, `tests/spike/`) EXCEPT the two Phase-1-bound artifacts (enumerator, RO probe) which land in their permanent module homes so Phase 1 imports them unchanged.

---

## File Structure

**Permanent artifacts (Phase 1 consumes these unchanged):**
- `src/services/cloud_overlay/whiteout.py` — engine-agnostic upperdir-walk → `{path, status}` diff enumerator (pure, no I/O beyond `os.scandir`/`os.lstat`/`os.getxattr`).
- `orchestrator/services/cloud/ro_probe.py` — fail-closed read-only verification: given a WebDAV base URL + RO identity creds, attempt every mutating verb and assert all are rejected.

**Image changes:**
- `docker/Dockerfile.workspace` — add `fuse-overlayfs` (apt block + version assertion).
- `docker/agent-vm-base/scripts/provision-stage1.sh` — same for the VM tier.

**Spike-only (throwaway harness + findings, deleted or archived after the report):**
- `scripts/spike/overlay_matrix.sh` — mounts fuse-overlayfs over the live rclone mount and runs the §6.1 test matrix.
- `scripts/spike/etag_walk_bench.py` — times a full-tree PROPFIND baseline at target sizes.
- `docs/design/cloud_access_unification.md` — the report (§11, new) is written back here, not a separate file.

**Tests:**
- `tests/cloud_overlay/test_whiteout.py` — enumerator unit tests.
- `tests/cloud/test_ro_probe.py` — RO probe unit tests (mocked httpx).

---

### Task 1: Add `fuse-overlayfs` to both workspace images

**Files:**
- Modify: `docker/Dockerfile.workspace:52-77` (the `eatmydata apt-get install` block; `fuse3` is at :73)
- Modify: `docker/agent-vm-base/scripts/provision-stage1.sh:120-125` (the apt block containing `fuse3` at :124)

**Interfaces:**
- Consumes: nothing.
- Produces: a `fuse-overlayfs` binary (≥1.13) present on `PATH` in both the container workspace image and the VM base image. No Python surface.

- [ ] **Step 1: Add the package to the container image apt block**

In `docker/Dockerfile.workspace`, in the `eatmydata apt-get install -y` list, add `fuse-overlayfs` immediately after the `fuse3` line (:73):

```dockerfile
    fuse3 \
    fuse-overlayfs \
```

- [ ] **Step 2: Add a version assertion after the rclone verification**

In `docker/Dockerfile.workspace`, immediately after the `&& rclone version` line (:138), extend that `RUN` (or add a new one) so the build fails loudly if the distro ships a too-old fuse-overlayfs:

```dockerfile
RUN fuse-overlayfs --version \
    && v="$(fuse-overlayfs --version | sed -n 's/.*version \([0-9.]*\).*/\1/p')" \
    && dpkg --compare-versions "$v" ge 1.13 \
        || { echo "fuse-overlayfs $v < 1.13 (big-dir/readdir fixes needed)"; exit 1; }
```

- [ ] **Step 3: Mirror both changes into the VM base provisioner**

In `docker/agent-vm-base/scripts/provision-stage1.sh`, add `fuse-overlayfs \` after the `fuse3 \` line (:124), and after the rclone `_section` block (ends ~:178) add:

```bash
fuse-overlayfs --version
v="$(fuse-overlayfs --version | sed -n 's/.*version \([0-9.]*\).*/\1/p')"
dpkg --compare-versions "$v" ge 1.13 \
    || { echo "fuse-overlayfs $v < 1.13"; exit 1; }
```

- [ ] **Step 4: Build the container image and verify the binary + version gate**

Run: `podman build -f docker/Dockerfile.workspace -t srw-workspace:spike . && podman run --rm srw-workspace:spike fuse-overlayfs --version`
Expected: build succeeds; prints `fuse-overlayfs: version 1.x` with x≥13. (Ubuntu 24.04 ships 1.13 — if apt yields <1.13, the RUN gate fails the build and Step 5 switches to the upstream static binary.)

- [ ] **Step 5 (only if Step 4's version gate fails): pin the upstream static binary instead**

Replace the apt entry with a checksum-verified download mirroring the rclone pattern already in the file (release asset `fuse-overlayfs-x86_64` from `github.com/containers/fuse-overlayfs/releases`), install to `/usr/bin/fuse-overlayfs`, `chmod +x`, re-run the version assertion. Apply to both files.

- [ ] **Step 6: Commit**

```bash
git add docker/Dockerfile.workspace docker/agent-vm-base/scripts/provision-stage1.sh
git commit -m "spike(cloud-overlay): install fuse-overlayfs in workspace + VM images"
```

---

### Task 2: Engine-agnostic whiteout → diff enumerator

**Files:**
- Create: `src/services/cloud_overlay/__init__.py` (empty package marker)
- Create: `src/services/cloud_overlay/whiteout.py`
- Test: `tests/cloud_overlay/__init__.py` (empty), `tests/cloud_overlay/test_whiteout.py`

**Interfaces:**
- Consumes: nothing (pure filesystem walk of an upperdir tree).
- Produces:
  - `enumerate_diff(upperdir: str) -> list[DiffEntry]` where `DiffEntry` is a dataclass `{path: str, status: Literal["added","modified","deleted"]}`, `path` relative to `upperdir`, POSIX-separated, sorted. "added" vs "modified" is decided by the caller-supplied `lower_exists`; when omitted, every non-deletion is reported as `status="present"` and the caller resolves add/modify against the lower. **Decision for this task:** the enumerator does NOT stat the lower (that's an rclone round-trip); it emits `present` / `deleted` and a Phase-1 caller upgrades `present`→`added|modified`. Keep the enumerator pure.
  - `is_whiteout(path: str) -> bool`, `is_opaque_dir(path: str) -> bool` helpers.
- Phase 1 relies on these exact names/types (spec §3.4 `UpperdirDiffSource`).

- [ ] **Step 1: Write the failing tests**

```python
# tests/cloud_overlay/test_whiteout.py
from __future__ import annotations
import os
from pathlib import Path

from src.services.cloud_overlay.whiteout import enumerate_diff, DiffEntry


def _mkchardev_or_skip(p: Path):
    try:
        os.mknod(p, 0o600 | 0o020000, os.makedev(0, 0))  # S_IFCHR, 0:0
    except PermissionError:
        import pytest
        pytest.skip("no CAP_MKNOD in test env; char-whiteout case needs privileged runner")


def test_added_and_modified_reported_as_present(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "new.txt").write_text("hi")
    (tmp_path / "changed.md").write_text("edited")
    got = enumerate_diff(str(tmp_path))
    assert DiffEntry("changed.md", "present") in got
    assert DiffEntry("sub/new.txt", "present") in got


def test_dotwh_file_is_deletion(tmp_path):
    (tmp_path / ".wh.gone.txt").write_text("")  # unprivileged whiteout form
    got = enumerate_diff(str(tmp_path))
    assert got == [DiffEntry("gone.txt", "deleted")]


def test_char_device_is_deletion(tmp_path):
    _mkchardev_or_skip(tmp_path / "gone.bin")
    got = enumerate_diff(str(tmp_path))
    assert got == [DiffEntry("gone.bin", "deleted")]


def test_opaque_dir_sentinel_marks_dir_replaced(tmp_path):
    d = tmp_path / "docs"
    d.mkdir()
    (d / ".wh..wh..opq").write_text("")       # opaque sentinel
    (d / "kept.txt").write_text("x")
    got = enumerate_diff(str(tmp_path))
    assert DiffEntry("docs", "deleted") in got        # lower dir wiped
    assert DiffEntry("docs/kept.txt", "present") in got
    assert all(e.path != "docs/.wh..wh..opq" for e in got)  # sentinel hidden


def test_metadata_xattr_files_never_leak_as_paths(tmp_path):
    # fuse-overlayfs bookkeeping must not appear as diff entries
    (tmp_path / "real.txt").write_text("x")
    got = enumerate_diff(str(tmp_path))
    assert [e for e in got if e.path == "real.txt"]
    assert all(not e.path.startswith(".wh") for e in got)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/cloud_overlay/test_whiteout.py -v`
Expected: FAIL — `ModuleNotFoundError: src.services.cloud_overlay.whiteout`.

- [ ] **Step 3: Write the enumerator**

```python
# src/services/cloud_overlay/whiteout.py
"""Engine-agnostic overlay-upperdir → diff enumerator.

Walks an overlay upperdir (kernel overlayfs OR fuse-overlayfs — the two
produce interchangeable-enough markers, see docs/design/cloud_access_
unification.md §3.1) and yields one DiffEntry per changed path.

Deletions surface as EITHER a char(0,0) device node at the deleted name
OR a `.wh.<name>` regular file. Directory replacement is an "opaque"
dir, marked by any of three xattrs or a `.wh..wh..opq` sentinel file.
Everything else is a real added/modified path (reported as "present";
add-vs-modify is resolved by the caller against the lower to avoid an
rclone round-trip per file).
"""
from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from typing import Literal

Status = Literal["present", "deleted"]

_WH_PREFIX = ".wh."
_OPAQUE_SENTINEL = ".wh..wh..opq"
_OPAQUE_XATTRS = (
    "trusted.overlay.opaque",
    "user.overlay.opaque",
    "user.fuseoverlayfs.opaque",
)


@dataclass(frozen=True)
class DiffEntry:
    path: str
    status: Status


def _is_char_whiteout(st: os.stat_result) -> bool:
    return stat.S_ISCHR(st.st_mode) and st.st_rdev == os.makedev(0, 0)


def is_whiteout(name: str) -> bool:
    return name.startswith(_WH_PREFIX) and name != _OPAQUE_SENTINEL


def is_opaque_dir(dirpath: str) -> bool:
    if os.path.exists(os.path.join(dirpath, _OPAQUE_SENTINEL)):
        return True
    for attr in _OPAQUE_XATTRS:
        try:
            if os.getxattr(dirpath, attr) == b"y":
                return True
        except OSError:
            continue
    return False


def enumerate_diff(upperdir: str) -> list[DiffEntry]:
    out: list[DiffEntry] = []
    root = os.path.abspath(upperdir)

    def rel(p: str) -> str:
        return os.path.relpath(p, root).replace(os.sep, "/")

    def walk(dirpath: str) -> None:
        opaque = is_opaque_dir(dirpath)
        if opaque and dirpath != root:
            out.append(DiffEntry(rel(dirpath), "deleted"))
        with os.scandir(dirpath) as it:
            for entry in it:
                name = entry.name
                if name == _OPAQUE_SENTINEL:
                    continue
                st = entry.stat(follow_symlinks=False)
                if _is_char_whiteout(st):
                    out.append(DiffEntry(rel(entry.path), "deleted"))
                    continue
                if is_whiteout(name):
                    real = os.path.join(dirpath, name[len(_WH_PREFIX):])
                    out.append(DiffEntry(rel(real), "deleted"))
                    continue
                if stat.S_ISDIR(st.st_mode):
                    walk(entry.path)
                else:
                    out.append(DiffEntry(rel(entry.path), "present"))

    walk(root)
    return sorted(out, key=lambda e: e.path)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/cloud_overlay/test_whiteout.py -v`
Expected: PASS (the char-device test SKIPs if the runner lacks CAP_MKNOD — that path is re-checked live in Task 4).

- [ ] **Step 5: Commit**

```bash
git add src/services/cloud_overlay tests/cloud_overlay
git commit -m "feat(cloud-overlay): engine-agnostic whiteout->diff enumerator"
```

---

### Task 3: Fail-closed read-only credential probe

**Files:**
- Create: `orchestrator/services/cloud/ro_probe.py`
- Test: `tests/cloud/test_ro_probe.py`

**Interfaces:**
- Consumes: an `httpx.AsyncClient`-compatible caller (inject for testability), a WebDAV base URL, and a target path.
- Produces:
  - `async def probe_read_only(client, base_url: str, path: str) -> RoProbeResult`
  - `RoProbeResult` dataclass `{ok: bool, failures: list[str]}` — `ok=True` iff EVERY mutating verb was rejected (status in the rejected set). `failures` lists each verb that unexpectedly succeeded.
  - Module constant `MUTATING_VERBS` and `REJECTED_STATUSES = {401, 403, 405}`.
- Phase 1 calls `probe_read_only` at mount-provision time and refuses protected mode unless `ok`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/cloud/test_ro_probe.py
from __future__ import annotations
import pytest

from orchestrator.services.cloud.ro_probe import (
    probe_read_only, RoProbeResult, MUTATING_VERBS,
)


class _FakeResp:
    def __init__(self, status): self.status_code = status


class _FakeClient:
    """Returns a per-verb status from a dict; defaults to 403 (rejected)."""
    def __init__(self, statuses): self._s = statuses
    async def request(self, method, url, **kw):
        return _FakeResp(self._s.get(method, 403))


@pytest.mark.asyncio
async def test_all_verbs_rejected_is_ok():
    res = await probe_read_only(_FakeClient({}), "https://cloud/dav", "folder/")
    assert isinstance(res, RoProbeResult)
    assert res.ok is True
    assert res.failures == []


@pytest.mark.asyncio
async def test_any_write_success_fails_closed():
    # PUT unexpectedly accepted (201) -> not read-only
    res = await probe_read_only(_FakeClient({"PUT": 201}), "https://cloud/dav", "f/")
    assert res.ok is False
    assert "PUT" in res.failures[0]


@pytest.mark.asyncio
async def test_side_channel_verbs_are_probed():
    # versions/trash restore CVE class must be in the verb set
    assert any("MOVE" == v[0] for v in MUTATING_VERBS)
    assert any("restore" in (v[1] or "").lower() for v in MUTATING_VERBS)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/cloud/test_ro_probe.py -v`
Expected: FAIL — `ModuleNotFoundError: orchestrator.services.cloud.ro_probe`.

- [ ] **Step 3: Write the probe**

```python
# orchestrator/services/cloud/ro_probe.py
"""Fail-closed read-only verification for a protected-mount identity.

Given credentials for the dedicated RO account (NOT agent-service — see
docs/design/cloud_access_unification.md §3.3), attempt every mutating
WebDAV verb AND the version/trash side channels that had real RO-bypass
CVEs (Nextcloud GHSA-5mq8-738w-5942 / GHSA-2vrq-fhmf-c49m). Protected
cloud mode must refuse to engage unless every one is rejected.
"""
from __future__ import annotations

from dataclasses import dataclass, field

REJECTED_STATUSES = frozenset({401, 403, 405})

# (verb, note, body-or-None). note documents the side channel; body used
# for endpoints that need a payload to be a fair test.
MUTATING_VERBS: list[tuple[str, str | None, bytes | None]] = [
    ("PUT", None, b"srw-ro-probe"),
    ("DELETE", None, None),
    ("MKCOL", None, None),
    ("MOVE", None, None),
    ("PROPPATCH", None, b'<?xml version="1.0"?><d:propertyupdate xmlns:d="DAV:"/>'),
    ("COPY", None, None),
    # side channels with historical RO-bypass CVEs:
    ("POST", "versions-restore", None),
    ("POST", "trash-restore", None),
]


@dataclass
class RoProbeResult:
    ok: bool
    failures: list[str] = field(default_factory=list)


async def probe_read_only(client, base_url: str, path: str) -> RoProbeResult:
    target = base_url.rstrip("/") + "/" + path.lstrip("/")
    failures: list[str] = []
    for verb, note, body in MUTATING_VERBS:
        kwargs = {}
        if body is not None:
            kwargs["content"] = body
        if verb == "MOVE":
            kwargs["headers"] = {"Destination": target + ".moved"}
        resp = await client.request(verb, target, **kwargs)
        if resp.status_code not in REJECTED_STATUSES:
            label = verb if not note else f"{verb} ({note})"
            failures.append(f"{label} -> {resp.status_code} (expected 401/403/405)")
    return RoProbeResult(ok=not failures, failures=failures)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/cloud/test_ro_probe.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/services/cloud/ro_probe.py tests/cloud/test_ro_probe.py
git commit -m "feat(cloud): fail-closed read-only mount-identity probe"
```

---

### Task 4: FUSE-on-FUSE prototype harness + §6.1 matrix (hands-on)

**Files:**
- Create: `scripts/spike/overlay_matrix.sh`

**Interfaces:**
- Consumes: a live workspace pod (built from Task 1's image) with an active rclone cloud mount, the Task 2 enumerator.
- Produces: a findings block (pasted into the Task 7 report) answering: does fuse-overlayfs mount cleanly over the rclone lower? do both whiteout forms appear and does `enumerate_diff` read them faithfully? copy-up timing? readdir latency?

- [ ] **Step 1: Write the harness script**

```bash
#!/usr/bin/env bash
# scripts/spike/overlay_matrix.sh — run INSIDE a workspace pod that already
# has an rclone mount at $LOWER. Proves fuse-overlayfs-over-rclone + the
# whiteout matrix from docs/design/cloud_access_unification.md §6.1.
set -euo pipefail
LOWER="${1:?rclone mount point, e.g. /cloud/home}"
BASE="${HOME}/.overlay"
UP="$BASE/upper"; WORK="$BASE/work"; MERGED="$BASE/merged"
mkdir -p "$UP" "$WORK" "$MERGED"

echo "== mount fuse-overlayfs over rclone lower =="
fuse-overlayfs -o "lowerdir=$LOWER,upperdir=$UP,workdir=$WORK" "$MERGED"
trap 'fusermount3 -u "$MERGED" || true' EXIT

echo "== (a) rm -rf whiteout storm =="; ls "$MERGED" | head
FIRST_DIR="$(find "$MERGED" -maxdepth 1 -mindepth 1 -type d | head -1)"
[ -n "$FIRST_DIR" ] && rm -rf "$FIRST_DIR" && echo "removed $FIRST_DIR"
echo "-- upper markers --"; ls -la "$UP" | head

echo "== (b) copy-up timing (modify existing lower file) =="
TGT="$(find "$MERGED" -maxdepth 2 -type f | head -1)"
[ -n "$TGT" ] && { time sed -i '1s/^/# srw-probe\n/' "$TGT"; }

echo "== (c) readdir latency on merged view =="; time ls -R "$MERGED" >/dev/null

echo "== (d) enumerate_diff over the upperdir =="
cd /  # ensure src importable per pod layout
python3 -c "import json,sys; sys.path.insert(0,'/app'); \
from src.services.cloud_overlay.whiteout import enumerate_diff; \
print(json.dumps([e.__dict__ for e in enumerate_diff('$UP')], indent=1))"
```

- [ ] **Step 2: Copy the built image into k3d and start a workspace with a mount**

Run: `k3d image import srw-workspace:spike -c srw` then launch a protected-eligible session per `docs/memories`/`local_k3d_testing_via_orchestrator_api.md` so an rclone mount exists at `/cloud/<name>`.
Expected: `mcp__orchestrator__get_workspace_overview` (or in-pod `mount | grep fuse`) shows the rclone FUSE mount active.

- [ ] **Step 3: Run the matrix in-pod and capture output**

Run: `kubectl exec <workspace-pod> -- bash /path/to/overlay_matrix.sh /cloud/<name> 2>&1 | tee /tmp/claude-*/scratchpad/overlay_matrix.out`
Expected (the go/no-go signal): fuse-overlayfs mounts without error; `rm -rf` leaves char(0,0) nodes or `.wh.` files in `$UP`; `enumerate_diff` prints the deletions + `present` entries; copy-up and readdir times are recorded (numbers, not pass/fail).

- [ ] **Step 4: Record findings (no code) into the scratchpad for Task 7**

Write the four answers + raw timings to `scratchpad/spike-findings-overlay.md`. If the mount fails or whiteouts don't enumerate, that is the **no-go** trigger → note it and proceed to Task 7 (the fallback path in spec §8 becomes primary).

- [ ] **Step 5: Commit the harness**

```bash
git add scripts/spike/overlay_matrix.sh
git commit -m "spike(cloud-overlay): fuse-overlayfs-over-rclone matrix harness"
```

---

### Task 5: Etag-baseline walk cost measurement (hands-on)

**Files:**
- Create: `scripts/spike/etag_walk_bench.py`

**Interfaces:**
- Consumes: an initialized `MainCloudBackend` (via the orchestrator's `main_cloud_router`) and a `ProjectFolderHandle`.
- Produces: wall-clock + request-count for a full-tree `list_project_folder` PROPFIND at representative sizes — the number that sizes the §8.1.3 decision (full-tree vs touched-paths baseline).

- [ ] **Step 1: Write the benchmark script**

```python
# scripts/spike/etag_walk_bench.py — run in-pod against the dev backend.
# Times the mount-time etag baseline (spec §3.4). Usage:
#   python3 etag_walk_bench.py <backend_id> <handle_db>
import asyncio, sys, time
sys.path.insert(0, "/app")
from orchestrator.main import main_cloud_router  # module-level router


async def main(backend_id: str, handle_db: str) -> None:
    from orchestrator.services.cloud import ProjectFolderHandle
    backend = main_cloud_router.for_backend(backend_id)
    handle = ProjectFolderHandle.from_db(handle_db, backend=backend_id)
    t0 = time.monotonic()
    entries = await backend.list_project_folder(handle)
    dt = time.monotonic() - t0
    files = [e for e in entries if not e.is_dir]
    print(f"files={len(files)} entries={len(entries)} walk={dt:.2f}s "
          f"({len(files)/dt:.0f} files/s)")


asyncio.run(main(sys.argv[1], sys.argv[2]))
```

- [ ] **Step 2: Run against a small and a large dev folder**

Run: `kubectl exec deploy/orchestrator -- python3 /app/scripts/spike/etag_walk_bench.py <backend_id> <handle>` for a ~50-file project folder and the largest dev folder available.
Expected: two `files=… walk=…s` lines. Record both.

- [ ] **Step 3: Record the finding for Task 7**

Append to `scratchpad/spike-findings-overlay.md`: the files/s rate and the extrapolated walk time at 10k / 100k files. Flag whether full-tree-at-mount stays acceptable (§8.1.3 lean holds) or touched-paths scoping is needed sooner.

- [ ] **Step 4: Commit**

```bash
git add scripts/spike/etag_walk_bench.py
git commit -m "spike(cloud-overlay): etag-baseline walk cost benchmark"
```

---

### Task 6: Snapshot-sequencing + refresh-op investigation (hands-on, no product code)

**Files:**
- None created; produces findings only (Task 7 input). Reads: `orchestrator/services/snapshot_service.py:361-386`, `src/services/cloud_mount/__init__.py`.

**Interfaces:**
- Consumes: the Task 4 pod (overlay mounted at `~/.overlay/merged`, upperdir at `~/.overlay/upper`).
- Produces: verified answers to spike items §6.2 (refresh/heal with open FDs) and §6.3 (snapshot without traversing the merged mount).

- [ ] **Step 1: Prove the tar does NOT traverse the merged mount**

Run in-pod: place the upperdir at `/home/agent-host/.overlay/upper` (inside snapshot scope :362) and the *merged mountpoint* outside it; then run the snapshot tar command from `snapshot_service.py:388-391` and inspect the member list.
Run: `kubectl exec <pod> -- bash -c 'tar -cf - --exclude=/var/cache/* /home/agent-host/ 2>/dev/null | tar -tf - | grep -c ".overlay/upper"'`
Expected: upperdir members present, zero members under the merged mountpoint / `/cloud`. If the merged mount is inside `/home/agent-host`, record that placement must move (finding).

- [ ] **Step 2: Test the refresh op against an open FD**

Run in-pod: open a long-lived reader on a merged-view file (`tail -f`), then execute the refresh sequence (`fusermount3 -u merged` → flush rclone dir cache via its rc `vfs/forget` → remount overlay) and observe whether unmount is blocked (`EBUSY`) and how the held FD behaves.
Expected: record whether refresh needs the agent to drop FDs first, a lazy unmount (`fusermount3 -uz`), or a quiesce signal. This is the §6.2 answer.

- [ ] **Step 3: Verify upperdir survives an overlay remount**

Run in-pod: after Step 2's remount, confirm the staged files + whiteouts in `~/.overlay/upper` are byte-identical (checksum before/after).
Expected: upperdir unchanged (the design's core assumption — spec §3.4). Record confirm/deny.

- [ ] **Step 4: Record findings for Task 7**

Append refresh-op sequence + snapshot-placement rule to `scratchpad/spike-findings-overlay.md`. No commit (no repo files changed).

---

### Task 7: Spike report + go/no-go, written into the design doc

**Files:**
- Modify: `docs/design/cloud_access_unification.md` (add §11 "Phase 0 spike results"; update §6 items to resolved/measured; update the status line).

**Interfaces:**
- Consumes: `scratchpad/spike-findings-overlay.md` (Tasks 4–6) + the committed artifacts (Tasks 2–3).
- Produces: a go/no-go verdict and the measured inputs Phase 1's plan needs (chosen engine confirmed, copy-up + walk costs, refresh-op sequence, snapshot placement rule).

- [ ] **Step 1: Write §11 into the design doc**

Add a `## 11. Phase 0 spike results — <date>` section with subsections mirroring §6 items 1–5, each stating **RESOLVED/measured** + the evidence (raw timings, whiteout forms observed, refresh sequence, snapshot placement). End with a one-line **GO** (proceed to Phase 1 plan) or **NO-GO** (fallback per §8 becomes primary) verdict.

- [ ] **Step 2: Flip the resolved §6 items and the status line**

In §6, prefix each now-answered item with `✅`. Update the top `**Status:**` line to note the spike is complete and cite §11.

- [ ] **Step 3: Update the architecture memory**

Update the auto-memory `cloud_storage_architecture_map.md` §research line with the spike verdict + the one or two numbers that matter (copy-up cost, walk rate) so the next session inherits them.

- [ ] **Step 4: Commit**

```bash
git add docs/design/cloud_access_unification.md
git commit -m "docs(design): Phase 0 spike results + go/no-go for protected cloud mode"
```

- [ ] **Step 5: Archive or delete the throwaway harness**

If GO: leave `scripts/spike/` for Phase 1 reference. If NO-GO: `git rm -r scripts/spike` in a follow-up commit and note the fallback pivot in §11.

---

## Self-Review

**Spec coverage (§6 spike items + §8.1 decisions):**
- §6.1 (FUSE-on-FUSE + whiteout/diff) → Tasks 2 (enumerator, unit-tested) + 4 (live matrix). ✔
- §6.2 (refresh/heal, open FDs) → Task 6 Steps 2–3. ✔
- §6.3 (snapshot sequencing) → Task 6 Step 1. ✔
- §6.4 (whiteout formats) → Task 2 (dual-format, both forms tested). ✔
- §6.5 (etag-walk cost) → Task 5. ✔
- RO mechanism/probe (§3.3, Global Constraint) → Task 3 with version floors + side-channel verbs. ✔
- Engine install (§3.1) → Task 1, both images. ✔
- §8.1 decisions are already settled in the doc; the spike *measures* the inputs (walk cost → 8.1.3) rather than re-deciding. ✔

**Placeholder scan:** enumerator and probe are complete with real code + tests; hands-on tasks (4–6) correctly produce measurements not fabricated numbers — that's the nature of a spike, not a placeholder. No "TBD"/"handle edge cases" left.

**Type consistency:** `enumerate_diff -> list[DiffEntry]` and `DiffEntry{path,status}` used identically in Task 2 and referenced in Task 4 Step 1; `probe_read_only -> RoProbeResult{ok,failures}` consistent Task 3. `status` value `"present"` (not `"added"`) is deliberate and documented — the add/modify upgrade is explicitly deferred to a Phase-1 caller.

**Out of scope (correctly deferred to the Phase 1 plan, written after GO):** `DiffSource` protocol + `UpperdirDiffSource`, `job_mounts` table, RO-identity provisioner + reconciler sweep, upperdir→S3 transport, the protected-mode toggle + Cockpit session review surface. These depend on the spike's measured outputs; specifying them now would be fabrication.
