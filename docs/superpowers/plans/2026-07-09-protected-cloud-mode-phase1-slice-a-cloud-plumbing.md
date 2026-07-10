# Protected Cloud Mode — Phase 1, Slice A (Orchestrator Cloud Plumbing) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the orchestrator-side control-plane foundation for protected cloud mode — per-user read-only reader identities, per-mount RO grants with a fail-closed engage gate, a mount-time etag baseline capture, and the reader/grant GC reconciler — all unit-testable with mocked HTTP/DB and no FUSE, so it ships and is verified in CI ahead of the mount/overlay work.

**Architecture:** This slice touches only `orchestrator/` (Python 3.12, FastAPI monolith `orchestrator/main.py`, cloud backends under `orchestrator/services/cloud/`, `PostgresDB` in `orchestrator/database/postgres.py`). It adds: (1) a feature flag; (2) a fix to the shared PROPFIND walker's double-subdir bug; (3) an "infinity-first" etag-baseline capture; (4) a `cloud_ro_mounts` DB table + CRUD; (5)+(6) per-backend RO-reader provisioning (Nextcloud fully; OpenCloud to the same interface); (7) the engage gate that wires the existing `ro_probe.py` module with a canary fixture; (8) a leader-gated reconciler that revokes orphaned grants. No agent, no `src/`, no Cockpit, no mount code — those are Slices B and C (outlined at the end).

**Tech Stack:** Python 3.12, `httpx` (backends speak WebDAV/OCS/LibreGraph), `pytest` + `pytest-asyncio`, `httpx.MockTransport` fakes (the established backend-test pattern — see `tests/cloud/test_nextcloud.py`), asyncpg (mocked-connection pattern — see `tests/test_thread_db.py`), SQL migrations under `orchestrator/database/migrations/app/`, Helm chart under `helm/`.

## Global Constraints

- **RO backend version floors (verbatim from design §3.3):** Nextcloud server **≥ 28.0.3**, groupfolders **≥ 20.1.2**. The RO engage gate must treat a lower version as fail-closed. (Already encoded in `ro_probe.VERSION_FLOORS`; the gate must call `check_version_floors`.)
- **RO enforcement lives in the share/role layer, never the token.** The mount identity is a **dedicated low-privilege account** (`srw-reader-<user_key>`), NOT `agent-service`. `agent-service` holds editor grants elsewhere, so an extracted `agent-service` credential could write other folders (design §3.3, §8.1.4).
- **Fail-closed everywhere.** The engage gate refuses (does not mount) on any probe failure, any inconclusive check, any skipped check, or a version below floor. `RoProbeResult.ok` is already this strict AND (`not (failures or skipped or inconclusive)`); do not weaken it.
- **Per-user readers, per-mount grants, per-provision credentials** (design §8.1.4): the `srw-reader-<user_key>` account is idempotently ensured (keyed by `user_id`); grants (NC group→folder read ACL / OC Space Viewer) are minted **per mount** and revoked at teardown **plus a periodic reconciler sweep** — never trust revoke-on-teardown alone. NC credentials rotate per provision; OC uses a short-TTL bearer.
- **Security invariant:** a credential present in (eventually) a workspace must grant no capability beyond that workspace's legitimate scope. Provisioning code must never grant the reader anything but read on the single target folder/Space.
- **Work on `develop` directly; do not push without asking** (project convention). Commit per task.
- **CI is the gate, not local pytest.** CI runs `pytest tests/ -x -q` on Python 3.12 (`.github/workflows/develop.yml`); it has **no `/dev/fuse`, no privileged containers, no live cloud backend**. Every test in this slice must pass with mocked `httpx`/asyncpg. Live NC/OpenCloud validation of the probe is a documented **manual** step (design §11.4), not a CI test.
- **`schema_current.sql` is GENERATED — never hand-edit it.** Add migrations under `orchestrator/database/migrations/app/`; regenerate the snapshot with `scripts/schema-snapshot.sh app`. CI (`db-migrations.yml`) enforces the snapshot matches.
- **Encrypt stored credentials.** Reuse `_encrypt_optional` / `_decrypt_stored` from `orchestrator/database/postgres.py` for the RO-mount credential column; never store a plaintext password.

---

## File Structure

**New files:**
- `orchestrator/services/cloud/etag_baseline.py` — engine-agnostic "infinity-first, concurrent-BFS-fallback" path→etag capture (pure orchestration over an injected PROPFIND callable). Task A3.
- `orchestrator/services/cloud/ro_engage.py` — the fail-closed engage gate: version floors → ensure reader → mint grant → seed canary → probe → persist-or-refuse. Task A7.
- `orchestrator/database/migrations/app/0050_cloud_ro_mounts.sql` — the per-mount reader/grant tracking table. Task A4.
- `tests/cloud/test_etag_baseline.py` — Task A3.
- `tests/cloud/test_ro_reader_nextcloud.py` — Task A5.
- `tests/cloud/test_ro_reader_opencloud.py` — Task A6.
- `tests/cloud/test_ro_engage.py` — Task A7.
- `tests/test_cloud_ro_mounts_db.py` — Task A4 (mocked-conn CRUD).
- `tests/test_ro_reader_reconciler.py` — Task A8.

**Modified files:**
- `helm/values.yaml`, `helm/templates/configmap.yaml`, `helm/templates/orchestrator/deployment.yaml` — feature flag. Task A1.
- `orchestrator/main.py` — flag reader helper (A1); reconciler loop wiring (A8).
- `orchestrator/services/cloud/_propfind.py` — self-entry drop generalized. Task A2.
- `orchestrator/services/cloud/nextcloud.py`, `orchestrator/services/cloud/opencloud.py` — walker self-drop fix (A2); `capture_etag_baseline` wiring (A3); RO-reader trio (A5/A6).
- `orchestrator/services/cloud/base.py` — `SupportsRoReader` protocol + `RoReaderGrant`/`CanaryFixture` dataclasses. Task A5.
- `orchestrator/database/postgres.py` — `cloud_ro_mounts` CRUD. Task A4.
- `orchestrator/database/schema_current.sql` — regenerated (not hand-edited). Task A4.
- `tests/cloud/test_propfind.py`, `tests/cloud/test_nextcloud.py` — regression tests. Task A2.

---

### Task A1: Feature flag `PROTECTED_CLOUD_MODE_ENABLED`

**Files:**
- Modify: `helm/values.yaml` (the `agent:` flag block, ~:175-185 alongside `skillsDbEnabled`)
- Modify: `helm/templates/configmap.yaml` (~:79-86 alongside `SKILLS_DB_ENABLED`)
- Modify: `helm/templates/orchestrator/deployment.yaml` (~:114-118, the `configMapKeyRef` block)
- Modify: `orchestrator/main.py` (add reader near `_is_skills_db_enabled` at ~:1117)
- Modify: `deployment/values-experimental.yaml` (~:261, dev-ON override)
- Test: `tests/test_protected_cloud_flag.py` (create)

**Interfaces:**
- Consumes: nothing.
- Produces: `orchestrator.main._is_protected_cloud_mode_enabled() -> bool` — reads env `PROTECTED_CLOUD_MODE_ENABLED`, true iff value lowercased/stripped ∈ {"true","1","yes"}. Later tasks (A7 engage gate, A8 reconciler, Slice C endpoints) gate on this.

- [ ] **Step 1: Write the failing test**

Create `tests/test_protected_cloud_flag.py`:

```python
from __future__ import annotations

import importlib

import pytest


@pytest.mark.parametrize(
    "value,expected",
    [
        ("true", True), ("TRUE", True), (" 1 ", True), ("yes", True),
        ("false", False), ("0", False), ("", False), ("off", False),
    ],
)
def test_protected_cloud_mode_flag_parsing(monkeypatch, value, expected):
    monkeypatch.setenv("PROTECTED_CLOUD_MODE_ENABLED", value)
    main = importlib.import_module("orchestrator.main")
    assert main._is_protected_cloud_mode_enabled() is expected


def test_protected_cloud_mode_flag_absent_defaults_false(monkeypatch):
    monkeypatch.delenv("PROTECTED_CLOUD_MODE_ENABLED", raising=False)
    main = importlib.import_module("orchestrator.main")
    assert main._is_protected_cloud_mode_enabled() is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_protected_cloud_flag.py -v`
Expected: FAIL with `AttributeError: module 'orchestrator.main' has no attribute '_is_protected_cloud_mode_enabled'`.

- [ ] **Step 3: Add the reader helper**

In `orchestrator/main.py`, immediately after `_is_skills_db_enabled` (~:1117), add:

```python
def _is_protected_cloud_mode_enabled() -> bool:
    """Whether protected cloud mode (RO-reader provisioning + capture overlay)
    is enabled for this deployment. Dev-ON / prod-OFF via the helm
    `agent.protectedCloudModeEnabled` flag (design §11 / cloud_access_unification)."""
    return os.getenv("PROTECTED_CLOUD_MODE_ENABLED", "").lower().strip() in (
        "true",
        "1",
        "yes",
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_protected_cloud_flag.py -v`
Expected: PASS (all cases).

- [ ] **Step 5: Wire the helm chart**

In `helm/values.yaml`, in the `agent:` block next to `skillsDbEnabled: "false"`, add:

```yaml
  # Protected cloud mode: RO-reader provisioning + capture overlay (dev-ON/prod-OFF).
  # See docs/design/cloud_access_unification.md §8 Phase 1.
  protectedCloudModeEnabled: "false"
```

In `helm/templates/configmap.yaml`, next to the `SKILLS_DB_ENABLED` line (~:86), add:

```yaml
  PROTECTED_CLOUD_MODE_ENABLED: {{ .Values.agent.protectedCloudModeEnabled | default "false" | quote }}
```

In `helm/templates/orchestrator/deployment.yaml`, next to the `SKILLS_DB_ENABLED` env block (~:114-118), add:

```yaml
            - name: PROTECTED_CLOUD_MODE_ENABLED
              valueFrom:
                configMapKeyRef:
                  name: {{ include "srw.fullname" . }}-config
                  key: PROTECTED_CLOUD_MODE_ENABLED
```

In `deployment/values-experimental.yaml`, next to `skillsDbEnabled: "true"` (~:261), add:

```yaml
  protectedCloudModeEnabled: "true"
```

- [ ] **Step 6: Verify the chart renders**

Run: `helm template ./helm -f deployment/values-experimental.yaml --show-only templates/orchestrator/deployment.yaml | grep -A4 PROTECTED_CLOUD_MODE_ENABLED`
Expected: shows the env var wired to the configMapKeyRef.
Run: `helm lint ./helm`
Expected: `1 chart(s) linted, 0 chart(s) failed`.

- [ ] **Step 7: Commit**

```bash
git add helm/values.yaml helm/templates/configmap.yaml helm/templates/orchestrator/deployment.yaml deployment/values-experimental.yaml orchestrator/main.py tests/test_protected_cloud_flag.py
git commit -m "feat(cloud): PROTECTED_CLOUD_MODE_ENABLED feature flag (dev-ON/prod-OFF)"
```

---

### Task A2: Fix the `_propfind` double-subdir bug

The shared PROPFIND parser drops the walk-root's self-href only when the stripped path is empty. On each `Depth: 1` PROPFIND of a subdirectory, that subdir's own self-href strips to a **non-empty** path, so the walker appends it again (it was already emitted as a child of its parent) — every subdirectory appears twice. This must be fixed before the etag baseline (A3) builds path→etag maps, or the conflict gate double-processes directories (design §11.5, amendment #4).

**Files:**
- Modify: `orchestrator/services/cloud/_propfind.py:38-76` (`parse_propfind_entries`)
- Modify: `orchestrator/services/cloud/nextcloud.py:660-681` (the `list_project_folder` walk loop)
- Modify: `orchestrator/services/cloud/opencloud.py:783-804` (the `list_project_folder` walk loop)
- Test: `tests/cloud/test_propfind.py` (extend), `tests/cloud/test_nextcloud.py` (extend)

**Interfaces:**
- Consumes: nothing.
- Produces: `parse_propfind_entries(xml, *, href_prefix, self_path="")` — new optional `self_path` param (a folder-relative path, no leading slash); the entry whose stripped `rel_path` equals `self_path.strip("/")` is dropped in addition to the root. Backward compatible (default `""` = today's root-only drop). Both `list_project_folder` walkers pass `self_path=current`.

- [ ] **Step 1: Write the failing parser test**

In `tests/cloud/test_propfind.py`, add a body whose PROPFIND is *of a subdirectory* (its own self-href is a non-empty subpath) and assert the self-entry is dropped when `self_path` names it:

```python
# A Depth:1 PROPFIND OF the "Documents" subdir: the response's first entry is
# the subdir itself (self-href), followed by its children. Without self_path,
# the subdir re-appears (the double-subdir bug); with self_path="Documents"
# it is dropped.
SUBDIR_BODY = (
    '<?xml version="1.0"?>'
    '<d:multistatus xmlns:d="DAV:" xmlns:s="http://sabredav.org/ns">'
    "<d:response>"
    "<d:href>/remote.php/dav/groupfolders/agent-service/NC%20Validation%20Project/Documents/</d:href>"
    "<d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype>"
    "<d:getetag>&quot;dir456&quot;</d:getetag></d:prop>"
    "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
    "<d:response>"
    "<d:href>/remote.php/dav/groupfolders/agent-service/NC%20Validation%20Project/Documents/report.md</d:href>"
    "<d:propstat><d:prop><d:getcontentlength>12</d:getcontentlength>"
    "<d:resourcetype/><d:getetag>&quot;file789&quot;</d:getetag></d:prop>"
    "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
    "</d:multistatus>"
)


def test_self_path_drops_the_subdir_self_entry():
    paths = {
        e.path
        for e in parse_propfind_entries(
            SUBDIR_BODY, href_prefix=PREFIX, self_path="Documents"
        )
    }
    assert paths == {"Documents/report.md"}  # "Documents" self-entry dropped


def test_self_path_default_keeps_backward_compatible_root_drop():
    # No self_path → only the root (empty stripped path) is dropped; the
    # subdir self-entry survives (this is the pre-fix behavior the walker
    # must now suppress by passing self_path).
    paths = {
        e.path for e in parse_propfind_entries(SUBDIR_BODY, href_prefix=PREFIX)
    }
    assert paths == {"Documents", "Documents/report.md"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/cloud/test_propfind.py::test_self_path_drops_the_subdir_self_entry -v`
Expected: FAIL — `parse_propfind_entries() got an unexpected keyword argument 'self_path'`.

- [ ] **Step 3: Add the `self_path` param to the parser**

In `orchestrator/services/cloud/_propfind.py`, change the signature and the drop condition:

```python
def parse_propfind_entries(
    xml: str, *, href_prefix: str, self_path: str = ""
) -> list[ProjectFolderEntry]:
```

Update the docstring's final sentence to: `Entries pointing at the walk target itself — the root (empty path after stripping) or the subdirectory named by`` self_path ``— are dropped, so a Depth:1 walk does not re-emit a directory it already surfaced as a child of its parent.`

Replace the empty-path drop (lines 59-62) with:

```python
        rel_path = href[len(decoded_prefix) :].rstrip("/")
        if rel_path == self_path.strip("/"):
            # The walk target's own self-entry (root, or the subdir this
            # Depth:1 PROPFIND is expanding). Its parent walk already emitted
            # it; re-emitting here is the double-subdir bug (design §11.5).
            continue
```

- [ ] **Step 4: Run parser test to verify it passes**

Run: `pytest tests/cloud/test_propfind.py -v`
Expected: PASS (new tests + the pre-existing ones unchanged).

- [ ] **Step 5: Make both walkers pass `self_path=current`**

In `orchestrator/services/cloud/nextcloud.py`, the walk loop calls `self._propfind_depth_one(url_path, href_prefix)` which internally calls `parse_propfind_entries(resp.text, href_prefix=href_prefix)`. Thread `current` through. Change `_propfind_depth_one`'s signature (nextcloud.py:683) to accept `self_path` and pass it:

```python
    async def _propfind_depth_one(
        self, url_path: str, href_prefix: str, *, self_path: str = ""
    ) -> list[ProjectFolderEntry]:
```

and at its return (nextcloud.py:717):

```python
        return parse_propfind_entries(
            resp.text, href_prefix=href_prefix, self_path=self_path
        )
```

At the call site inside `list_project_folder` (nextcloud.py:671), pass the current subpath and delete the now-redundant defensive re-append guard:

```python
            entries = await self._propfind_depth_one(
                url_path, href_prefix, self_path=current
            )
            for entry in entries:
                all_entries.append(entry)
                if entry.is_dir and entry.path not in seen:
                    queue.append(entry.path)
```

Apply the identical change to `orchestrator/services/cloud/opencloud.py`: `_propfind_depth_one` (opencloud.py:806) gains `self_path`, passes it to `parse_propfind_entries` (opencloud.py:~843 return), and the `list_project_folder` loop (opencloud.py:794-803) passes `self_path=current` and drops the re-append guard the same way.

- [ ] **Step 6: Write the walker regression test**

In `tests/cloud/test_nextcloud.py` (which already stands up `FakeNextcloud`), add a nested-tree walk that asserts no path is duplicated:

```python
@pytest.mark.asyncio
async def test_list_project_folder_does_not_double_count_subdirs(monkeypatch):
    backend = NextcloudBackend(_nc_test_settings())
    fake = FakeNextcloud()
    fake.dirs.update({"Documents", "Documents/Sub"})
    fake.files["Documents/a.md"] = b"a"
    fake.files["Documents/Sub/b.md"] = b"b"
    _install_fake(backend, fake)  # existing helper in this test module

    entries = await backend.list_project_folder(_handle())
    paths = [e.path for e in entries]
    assert len(paths) == len(set(paths)), f"duplicate paths: {paths}"
    assert set(paths) == {
        "Documents",
        "Documents/a.md",
        "Documents/Sub",
        "Documents/Sub/b.md",
    }
```

If `_install_fake` is not the exact helper name in the module, use whatever the existing tests use to bind the fake transport (grep the file for how `FakeNextcloud` is attached to the backend's `_client`).

- [ ] **Step 7: Run the full cloud suite**

Run: `pytest tests/cloud/test_propfind.py tests/cloud/test_nextcloud.py tests/cloud/test_opencloud.py -v`
Expected: PASS, including the pre-existing `test_parses_dir_and_file_entries` (root drop still works).

- [ ] **Step 8: Commit**

```bash
git add orchestrator/services/cloud/_propfind.py orchestrator/services/cloud/nextcloud.py orchestrator/services/cloud/opencloud.py tests/cloud/test_propfind.py tests/cloud/test_nextcloud.py
git commit -m "fix(cloud): drop subdir self-entry in PROPFIND walk (double-subdir bug, §11.5)"
```

---

### Task A3: Etag-baseline capture (infinity-first + concurrent-BFS fallback)

The overlay has no Mode-A Gitea seed, so `detect_external_mods`' path→etag input must come from a **mount-time PROPFIND walk** (design §3.4). Sequential depth-1 BFS does not scale past ~50-100 directories (design §11.5); the capture must try one `Depth: infinity` PROPFIND first (one request on Nextcloud) and fall back to a bounded-concurrency BFS when the backend rejects infinity (OpenCloud 400s it). Amendment #4.

**Files:**
- Create: `orchestrator/services/cloud/etag_baseline.py`
- Modify: `orchestrator/services/cloud/nextcloud.py` (add `capture_etag_baseline`; generalize propfind to accept a depth)
- Modify: `orchestrator/services/cloud/opencloud.py` (add `capture_etag_baseline`)
- Modify: `orchestrator/services/cloud/base.py` (declare `capture_etag_baseline` on the `MainCloudBackend` Protocol)
- Test: `tests/cloud/test_etag_baseline.py` (create), `tests/cloud/test_nextcloud.py` (extend)

**Interfaces:**
- Consumes: `parse_propfind_entries(..., self_path=...)` (A2); `ProjectFolderEntry` (`.path`, `.is_dir`, `.etag`).
- Produces:
  - `etag_baseline.PropfindError(Exception)` — raised by a propfind callable to signal "this depth is unsupported / failed" so the helper can fall back.
  - `etag_baseline.capture_etag_baseline(*, root_subpath, list_children, list_tree, concurrency=8) -> dict[str, str]` — pure async orchestration. `list_tree()` attempts one whole-tree read and returns `list[ProjectFolderEntry]` or raises `PropfindError`; `list_children(subpath) -> list[ProjectFolderEntry]` reads one directory's immediate children. Returns `{path: etag}` for **files only** (dirs excluded — matches Mode A's `entries_map`).
  - `MainCloudBackend.capture_etag_baseline(handle) -> dict[str, str]` on both backends — the wired entry point Slice B calls at mount time.

- [ ] **Step 1: Write the failing pure-helper test**

Create `tests/cloud/test_etag_baseline.py`:

```python
from __future__ import annotations

import pytest

from orchestrator.services.cloud.etag_baseline import (
    PropfindError,
    capture_etag_baseline,
)
from orchestrator.services.cloud.handles import ProjectFolderEntry


def _f(path, etag):
    return ProjectFolderEntry(path=path, is_dir=False, etag=etag)


def _d(path):
    return ProjectFolderEntry(path=path, is_dir=True, etag="dir-etag")


@pytest.mark.asyncio
async def test_infinity_path_used_when_list_tree_succeeds():
    tree = [_d("docs"), _f("docs/a.md", "e1"), _f("b.md", "e2")]
    calls = {"children": 0}

    async def list_tree():
        return tree

    async def list_children(_sub):
        calls["children"] += 1
        return []

    out = await capture_etag_baseline(
        root_subpath="", list_children=list_children, list_tree=list_tree
    )
    assert out == {"docs/a.md": "e1", "b.md": "e2"}  # files only, no dirs
    assert calls["children"] == 0  # infinity short-circuits the BFS


@pytest.mark.asyncio
async def test_falls_back_to_bfs_when_infinity_rejected():
    async def list_tree():
        raise PropfindError("Depth: infinity rejected (400)")

    tree = {
        "": [_d("docs"), _f("top.md", "e0")],
        "docs": [_d("docs/sub"), _f("docs/a.md", "e1")],
        "docs/sub": [_f("docs/sub/c.md", "e3")],
    }

    async def list_children(sub):
        return tree[sub]

    out = await capture_etag_baseline(
        root_subpath="", list_children=list_children, list_tree=list_tree
    )
    assert out == {"top.md": "e0", "docs/a.md": "e1", "docs/sub/c.md": "e3"}


@pytest.mark.asyncio
async def test_bfs_does_not_revisit_and_terminates_on_cycle_guard():
    async def list_tree():
        raise PropfindError("no infinity")

    # Malformed backend that returns the same dir as its own child; the seen
    # set must prevent an infinite loop.
    async def list_children(sub):
        if sub == "":
            return [ProjectFolderEntry(path="loop", is_dir=True, etag="d")]
        return [ProjectFolderEntry(path="loop", is_dir=True, etag="d")]

    out = await capture_etag_baseline(
        root_subpath="", list_children=list_children, list_tree=list_tree
    )
    assert out == {}  # no files, and it returned rather than hanging
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/cloud/test_etag_baseline.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'orchestrator.services.cloud.etag_baseline'`.

- [ ] **Step 3: Write the pure helper**

Create `orchestrator/services/cloud/etag_baseline.py`:

```python
"""Mount-time etag-baseline capture (design §3.4, §11.5).

Protected cloud mode has no Mode-A Gitea seed, so the path→etag map that
``detect_external_mods`` compares against live cloud state must be captured by
a PROPFIND walk when the overlay mounts. Sequential Depth:1 BFS does not scale
past ~50-100 directories, so this tries one ``Depth: infinity`` PROPFIND first
(one request on Nextcloud) and falls back to a bounded-concurrency BFS only
when the backend rejects infinity (OpenCloud 400s it — opencloud.py:763).

Pure orchestration: the caller injects the two PROPFIND primitives so this
module stays backend- and auth-agnostic and unit-testable without httpx.
"""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

from .handles import ProjectFolderEntry


class PropfindError(Exception):
    """A PROPFIND attempt failed or the depth is unsupported.

    Raised by ``list_tree`` to signal "infinity is not available, fall back to
    BFS". A ``list_children`` raising this aborts the capture (a mid-walk
    failure means an incomplete baseline, which must not be trusted).
    """


def _files_to_map(entries: list[ProjectFolderEntry]) -> dict[str, str]:
    # Files only: dir etags churn on any descendant change and the conflict
    # gate compares file content, matching Mode A's entries_map.
    return {e.path: e.etag for e in entries if not e.is_dir}


async def capture_etag_baseline(
    *,
    root_subpath: str,
    list_children: Callable[[str], Awaitable[list[ProjectFolderEntry]]],
    list_tree: Callable[[], Awaitable[list[ProjectFolderEntry]]],
    concurrency: int = 8,
) -> dict[str, str]:
    """Return ``{path: etag}`` for every file under ``root_subpath``.

    Tries ``list_tree()`` (one whole-tree read) first; on ``PropfindError``
    falls back to a bounded-concurrency breadth-first walk via
    ``list_children``.
    """
    try:
        return _files_to_map(await list_tree())
    except PropfindError:
        pass  # infinity unsupported — BFS below

    out: dict[str, str] = {}
    seen: set[str] = set()
    frontier = [root_subpath.strip("/")]
    sem = asyncio.Semaphore(concurrency)

    async def expand(sub: str) -> list[str]:
        async with sem:
            children = await list_children(sub)
        next_dirs: list[str] = []
        for e in children:
            if e.is_dir:
                if e.path not in seen:
                    next_dirs.append(e.path)
            else:
                out[e.path] = e.etag
        return next_dirs

    while frontier:
        level = [s for s in frontier if s not in seen]
        seen.update(level)
        results = await asyncio.gather(*(expand(s) for s in level))
        frontier = [d for group in results for d in group]
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/cloud/test_etag_baseline.py -v`
Expected: PASS (all three).

- [ ] **Step 5: Wire Nextcloud's `capture_etag_baseline`**

In `orchestrator/services/cloud/nextcloud.py`, generalize the propfind to take a depth and add the backend method. First, add a depth-parameterized propfind alongside `_propfind_depth_one` (reuse its body; add `depth` to the header). Add after `_propfind_depth_one`:

```python
    async def _propfind_at_depth(
        self, url_path: str, href_prefix: str, *, depth: str, self_path: str = ""
    ) -> list[ProjectFolderEntry]:
        """PROPFIND at an explicit Depth. ``depth="infinity"`` is used for the
        one-shot etag baseline; a non-207 (e.g. sabre's 403/400 on infinity)
        raises PropfindError so the caller falls back to Depth:1 BFS."""
        try:
            resp = await self._client.request(
                "PROPFIND",
                url_path,
                headers={"Depth": depth},
                auth=(self._agent_user, self._agent_password),
            )
        except httpx.HTTPError as e:
            raise PropfindError(str(e)) from e
        if resp.status_code != 207:
            raise PropfindError(
                f"PROPFIND Depth:{depth} returned {resp.status_code}"
            )
        return parse_propfind_entries(
            resp.text, href_prefix=href_prefix, self_path=self_path
        )

    async def capture_etag_baseline(
        self, handle: ProjectFolderHandle
    ) -> dict[str, str]:
        self._ensure_ready()
        base_path = self._groupfolder_dav_base(handle)
        href_prefix = f"{base_path}/"

        async def list_tree() -> list[ProjectFolderEntry]:
            return await self._propfind_at_depth(
                base_path, href_prefix, depth="infinity"
            )

        async def list_children(sub: str) -> list[ProjectFolderEntry]:
            safe_sub = quote(sub, safe="/")
            url_path = f"{base_path}/{safe_sub}" if safe_sub else base_path
            return await self._propfind_at_depth(
                url_path, href_prefix, depth="1", self_path=sub
            )

        return await capture_etag_baseline(
            root_subpath="", list_children=list_children, list_tree=list_tree
        )
```

Add the imports at the top of `nextcloud.py`:

```python
from .etag_baseline import PropfindError, capture_etag_baseline
```

- [ ] **Step 6: Wire OpenCloud's `capture_etag_baseline`**

In `orchestrator/services/cloud/opencloud.py`, add the same `_propfind_at_depth` (bearer-authed, mirroring its existing `_propfind_depth_one`) and `capture_etag_baseline` using `base_path = f"/dav/spaces/{safe_drive}"`. OpenCloud rejects `Depth: infinity` with 400, so `_propfind_at_depth(..., depth="infinity")` will raise `PropfindError` and the helper falls straight through to BFS — which is the intended per-backend behavior (no special-casing needed). Add the same import line.

- [ ] **Step 7: Declare it on the Protocol**

In `orchestrator/services/cloud/base.py`, add to the `MainCloudBackend` Protocol (near `list_project_folder`, ~:211):

```python
    async def capture_etag_baseline(
        self, handle: "ProjectFolderHandle"
    ) -> dict[str, str]:
        """Return ``{path: etag}`` for every file under the project folder, for
        the protected-mode conflict baseline (design §3.4). Infinity-first with
        a Depth:1 BFS fallback (§11.5)."""
        ...
```

- [ ] **Step 8: Write the backend integration test**

In `tests/cloud/test_nextcloud.py`, add a test that `FakeNextcloud` answers a `Depth: infinity` PROPFIND with the whole tree and `capture_etag_baseline` returns the file→etag map in one shot. If `FakeNextcloud` only handles `Depth: 1`, extend it: when the request header `Depth == "infinity"`, return a multistatus body covering every stored file/dir (so the infinity path is exercised); keep `Depth: 1` behavior for the walker test. Assert:

```python
@pytest.mark.asyncio
async def test_capture_etag_baseline_prefers_infinity(monkeypatch):
    backend = NextcloudBackend(_nc_test_settings())
    fake = FakeNextcloud()
    fake.files["a.md"] = b"a"
    fake.files["docs/b.md"] = b"b"
    fake.dirs.add("docs")
    _install_fake(backend, fake)
    base = await backend.capture_etag_baseline(_handle())
    assert set(base) == {"a.md", "docs/b.md"}  # files only
    assert all(v for v in base.values())  # etags populated
```

Also add a fallback test: make the fake return a non-207 (e.g. 400) for `Depth: infinity` and 207 for `Depth: 1`, then assert the same result is produced via BFS.

- [ ] **Step 9: Run the tests**

Run: `pytest tests/cloud/test_etag_baseline.py tests/cloud/test_nextcloud.py -v`
Expected: PASS.

- [ ] **Step 10: Commit**

```bash
git add orchestrator/services/cloud/etag_baseline.py orchestrator/services/cloud/nextcloud.py orchestrator/services/cloud/opencloud.py orchestrator/services/cloud/base.py tests/cloud/test_etag_baseline.py tests/cloud/test_nextcloud.py
git commit -m "feat(cloud): mount-time etag baseline capture (infinity-first + BFS fallback, §11.5)"
```

---

### Task A4: `cloud_ro_mounts` DB model + CRUD

Per-mount RO grant records: the reconciler (A8) needs a durable list of active grants keyed to their thread, with the credential (encrypted) and the serialized grant handle to revoke. One row per mount.

**Files:**
- Create: `orchestrator/database/migrations/app/0050_cloud_ro_mounts.sql`
- Modify: `orchestrator/database/postgres.py` (CRUD methods)
- Regenerate: `orchestrator/database/schema_current.sql` (via script — do NOT hand-edit)
- Test: `tests/test_cloud_ro_mounts_db.py` (create)

**Interfaces:**
- Consumes: `_encrypt_optional` / `_decrypt_stored` (postgres.py).
- Produces on `PostgresDB`:
  - `create_ro_mount(*, thread_id, user_id, backend, reader_id, grant_handle, credentials, webdav_url, auth_kind) -> str` (returns row id). Encrypts `credentials`. Upserts on `thread_id` (a re-provision replaces the prior grant row).
  - `get_ro_mount_by_thread(thread_id) -> dict | None` (decrypts `credentials`).
  - `list_active_ro_mounts() -> list[dict]` (status='active'; decrypts).
  - `list_ro_mounts_for_user(user_id) -> list[dict]`.
  - `mark_ro_mount_revoked(row_id) -> bool` (sets status='revoked', revoked_at=now()).

- [ ] **Step 1: Write the migration**

Create `orchestrator/database/migrations/app/0050_cloud_ro_mounts.sql`:

```sql
-- migration:     0052_cloud_ro_mounts.sql
-- description:   Per-mount read-only reader grants for protected cloud mode.
--                One row per protected session mount: the srw-reader-<user>
--                account, the serialized grant handle to revoke (NC group→
--                folder read ACL / OC Space Viewer permission id), and the
--                encrypted per-provision credential. The reconciler
--                (services: ro_reader_reconciler) sweeps status='active' rows
--                whose thread is gone and revokes the grant — never trust
--                revoke-on-teardown alone (design §8.1.4).
-- depends-on:    0001_initial.sql
-- expected:      < 1s (one empty CREATE TABLE on a fresh DB).
-- locks:         Brief ACCESS EXCLUSIVE (brand-new object).
-- transactional: yes
-- ============================================================================

CREATE TABLE IF NOT EXISTS cloud_ro_mounts (
    id           UUID         PRIMARY KEY DEFAULT public.uuid_generate_v4(),
    thread_id    UUID         NOT NULL,
    user_id      UUID         NOT NULL,
    backend      TEXT         NOT NULL,
    reader_id    TEXT         NOT NULL,
    grant_handle TEXT         NOT NULL,
    credentials  TEXT,                     -- encrypted app-password (NC); NULL for OC bearer
    webdav_url   TEXT         NOT NULL,
    auth_kind    TEXT         NOT NULL,
    status       TEXT         NOT NULL DEFAULT 'active',  -- active | revoked
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    revoked_at   TIMESTAMPTZ
);

COMMENT ON TABLE cloud_ro_mounts IS
    'Per-mount read-only reader grants for protected cloud mode. One row per '
    'protected session mount; the reconciler revokes active grants whose thread '
    'is gone. Credentials are encrypted at rest (postgres._encrypt_optional).';

-- One live grant per thread; a re-provision upserts in place.
CREATE UNIQUE INDEX cloud_ro_mounts_thread_idx ON cloud_ro_mounts (thread_id);
-- Reconciler sweep scans active rows.
CREATE INDEX cloud_ro_mounts_status_idx ON cloud_ro_mounts (status);
-- User-deletion GC.
CREATE INDEX cloud_ro_mounts_user_idx ON cloud_ro_mounts (user_id);
```

- [ ] **Step 2: Apply the migration locally and regenerate the snapshot**

Run: `python -m orchestrator.database.migrate --database app` (or the project's documented migrate entrypoint — check `orchestrator/database/migrate.py` for the exact CLI).
Expected: applies `0052_cloud_ro_mounts.sql` with no error.
Run: `scripts/schema-snapshot.sh app`
Expected: `orchestrator/database/schema_current.sql` now contains `CREATE TABLE public.cloud_ro_mounts`.

- [ ] **Step 3: Write the failing CRUD test**

Create `tests/test_cloud_ro_mounts_db.py`, following the mocked-connection pattern from `tests/test_thread_db.py` (`_make_db_with_conn`, `_mock_conn`). Copy those two helpers into this file (they are local, ~20 lines). Then:

```python
import pytest
from unittest.mock import AsyncMock

from orchestrator.database.postgres import PostgresDB, _decrypt_stored
# reuse _make_db_with_conn / _mock_conn — paste from tests/test_thread_db.py


@pytest.mark.asyncio
async def test_create_ro_mount_encrypts_credentials():
    conn = _mock_conn()
    conn.fetchval = AsyncMock(return_value="row-uuid")
    db = _make_db_with_conn(conn)
    row_id = await db.create_ro_mount(
        thread_id="11111111-1111-1111-1111-111111111111",
        user_id="22222222-2222-2222-2222-222222222222",
        backend="nextcloud",
        reader_id="srw-reader-abc",
        grant_handle='{"group_id":"g1","folder_id":"7"}',
        credentials="s3cr3t-app-pw",
        webdav_url="https://nc/remote.php/dav/files/srw-reader-abc/Proj/",
        auth_kind="basic",
    )
    assert row_id == "row-uuid"
    # The credential passed to SQL must be ciphertext, not the plaintext.
    args = conn.fetchval.call_args.args
    assert "s3cr3t-app-pw" not in args, "plaintext credential reached SQL"


@pytest.mark.asyncio
async def test_get_ro_mount_decrypts_credentials():
    from orchestrator.database.postgres import _encrypt_optional
    conn = _mock_conn()
    conn.fetchrow = AsyncMock(return_value={
        "id": "row-uuid",
        "thread_id": "t", "user_id": "u", "backend": "nextcloud",
        "reader_id": "srw-reader-abc",
        "grant_handle": "{}",
        "credentials": _encrypt_optional("s3cr3t-app-pw"),
        "webdav_url": "https://nc/...", "auth_kind": "basic",
        "status": "active", "created_at": None, "revoked_at": None,
    })
    db = _make_db_with_conn(conn)
    row = await db.get_ro_mount_by_thread("t")
    assert row["credentials"] == "s3cr3t-app-pw"
```

- [ ] **Step 4: Run test to verify it fails**

Run: `pytest tests/test_cloud_ro_mounts_db.py -v`
Expected: FAIL — `AttributeError: 'PostgresDB' object has no attribute 'create_ro_mount'`.

- [ ] **Step 5: Implement the CRUD methods**

In `orchestrator/database/postgres.py`, add (near other thread/cloud methods):

```python
    async def create_ro_mount(
        self,
        *,
        thread_id: str,
        user_id: str,
        backend: str,
        reader_id: str,
        grant_handle: str,
        credentials: str | None,
        webdav_url: str,
        auth_kind: str,
    ) -> str:
        """Upsert the per-mount RO grant row (one live grant per thread).
        Encrypts ``credentials`` at rest."""
        async with self.acquire() as conn:
            return await conn.fetchval(
                """
                INSERT INTO cloud_ro_mounts
                    (thread_id, user_id, backend, reader_id, grant_handle,
                     credentials, webdav_url, auth_kind, status)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,'active')
                ON CONFLICT (thread_id) DO UPDATE SET
                    user_id=$2, backend=$3, reader_id=$4, grant_handle=$5,
                    credentials=$6, webdav_url=$7, auth_kind=$8,
                    status='active', created_at=now(), revoked_at=NULL
                RETURNING id::text
                """,
                thread_id, user_id, backend, reader_id, grant_handle,
                _encrypt_optional(credentials), webdav_url, auth_kind,
            )

    async def get_ro_mount_by_thread(self, thread_id: str) -> dict | None:
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM cloud_ro_mounts WHERE thread_id=$1", thread_id
            )
        return self._ro_mount_row(row) if row else None

    async def list_active_ro_mounts(self) -> list[dict]:
        async with self.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM cloud_ro_mounts WHERE status='active'"
            )
        return [self._ro_mount_row(r) for r in rows]

    async def list_ro_mounts_for_user(self, user_id: str) -> list[dict]:
        async with self.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM cloud_ro_mounts WHERE user_id=$1", user_id
            )
        return [self._ro_mount_row(r) for r in rows]

    async def mark_ro_mount_revoked(self, row_id: str) -> bool:
        async with self.acquire() as conn:
            result = await conn.execute(
                "UPDATE cloud_ro_mounts SET status='revoked', revoked_at=now() "
                "WHERE id=$1 AND status='active'",
                row_id,
            )
        return result.endswith("1")

    @staticmethod
    def _ro_mount_row(row) -> dict:
        d = dict(row)
        d["credentials"] = _decrypt_stored(d.get("credentials"), field="cloud_ro_mounts.credentials")
        return d
```

- [ ] **Step 6: Run test to verify it passes**

Run: `pytest tests/test_cloud_ro_mounts_db.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add orchestrator/database/migrations/app/0050_cloud_ro_mounts.sql orchestrator/database/schema_current.sql orchestrator/database/postgres.py tests/test_cloud_ro_mounts_db.py
git commit -m "feat(cloud): cloud_ro_mounts table + CRUD for per-mount RO grants"
```

---

### Task A5: Nextcloud RO-reader provisioning

Provision the `srw-reader-<user_key>` account and mint/revoke a per-mount read-only grant on the target Group Folder. Nextcloud is the spike-validated backend (§11.7), so it is implemented fully here.

**Files:**
- Modify: `orchestrator/services/cloud/base.py` (add `SupportsRoReader` protocol + `RoReaderGrant`, `CanaryFixture` dataclasses)
- Modify: `orchestrator/services/cloud/nextcloud.py` (the reader trio + canary)
- Test: `tests/cloud/test_ro_reader_nextcloud.py` (create)

**Interfaces:**
- Consumes: `_ALL_PERMISSIONS`/`_grant_group_access` (nextcloud.py); `ensure_group`, `add_user_to_group`, `remove_user_from_group` (nextcloud.py); `ProjectFolderHandle`.
- Produces (in `base.py`):
  ```python
  @dataclass(frozen=True)
  class RoReaderGrant:
      reader_id: str        # native id of the srw-reader-<user_key> account
      grant_handle: str     # serialized JSON to pass back to revoke_ro_grant
      webdav_url: str       # RO WebDAV URL the mount uses
      credentials: str | None  # app-password (NC); None for OC bearer
      auth_kind: str        # "basic" | "keycloak_user_impersonation"

  @dataclass(frozen=True)
  class CanaryFixture:
      path: str                 # folder-relative path of the canary file
      version_ref: str | None   # real version id/href for versions-restore probe
      trash_ref: str | None     # real trashbin id for trash-restore probe

  class SupportsRoReader(Protocol):
      async def ensure_ro_reader(self, *, user_key: str) -> str: ...
      async def mint_ro_grant(self, handle, *, user_key: str, grant_key: str) -> RoReaderGrant: ...
      async def revoke_ro_grant(self, grant_handle: str, *, user_key: str) -> None: ...
      async def seed_canary_fixture(self, handle) -> CanaryFixture: ...
      async def remove_canary_fixture(self, handle, fixture: CanaryFixture) -> None: ...
  ```
- Produces (in `nextcloud.py`): concrete `NextcloudBackend` implementations of all five.

- [ ] **Step 1: Add the protocol + dataclasses**

In `orchestrator/services/cloud/base.py`, add the `RoReaderGrant`, `CanaryFixture` dataclasses and the `SupportsRoReader` Protocol exactly as in the Interfaces block above. Keep it a separate `@runtime_checkable Protocol` like the existing `SupportsRcloneMount` (capability protocol, not a required part of `MainCloudBackend`).

- [ ] **Step 2: Write the failing Nextcloud test**

Create `tests/cloud/test_ro_reader_nextcloud.py`. Extend the `FakeNextcloud` pattern (or add a focused fake) to answer the OCS user/group endpoints. Test the reader lifecycle end to end against the fake:

```python
@pytest.mark.asyncio
async def test_ensure_ro_reader_creates_low_priv_account():
    backend, fake = _backend_with_ocs_fake()
    reader_id = await backend.ensure_ro_reader(user_key="abc")
    assert reader_id == "srw-reader-abc"
    assert fake.users["srw-reader-abc"]["groups"] == []  # no folder access by default


@pytest.mark.asyncio
async def test_ensure_ro_reader_is_idempotent():
    backend, fake = _backend_with_ocs_fake()
    await backend.ensure_ro_reader(user_key="abc")
    # Second call tolerates OCS 102 "user already exists".
    reader_id = await backend.ensure_ro_reader(user_key="abc")
    assert reader_id == "srw-reader-abc"


@pytest.mark.asyncio
async def test_mint_grant_gives_reader_read_only_on_folder():
    backend, fake = _backend_with_ocs_fake()
    await backend.ensure_ro_reader(user_key="abc")
    grant = await backend.mint_ro_grant(_handle(), user_key="abc", grant_key="thread-1")
    # Reader is now in a per-mount group that has READ (permission=1) on the folder.
    group = _json(grant.grant_handle)["group_id"]
    assert fake.folder_group_perms["7"][group] == 1  # read-only, not 31
    assert group in fake.users["srw-reader-abc"]["groups"]
    assert grant.credentials  # a rotated app-password was issued
    assert grant.auth_kind == "basic"


@pytest.mark.asyncio
async def test_revoke_grant_removes_folder_access_but_keeps_account():
    backend, fake = _backend_with_ocs_fake()
    await backend.ensure_ro_reader(user_key="abc")
    grant = await backend.mint_ro_grant(_handle(), user_key="abc", grant_key="thread-1")
    await backend.revoke_ro_grant(grant.grant_handle, user_key="abc")
    group = _json(grant.grant_handle)["group_id"]
    assert group not in fake.folder_group_perms.get("7", {})
    assert "srw-reader-abc" in fake.users  # account survives
```

(`_backend_with_ocs_fake`, `_json`, `_handle` are small local helpers you write; model the OCS fake on the existing `FakeNextcloud` — it needs `users`, `groups`, `folder_group_perms` dicts and to answer `POST /ocs/v2.php/cloud/users`, `POST .../groups`, group-folder grant, user password `PUT`, and membership add/remove.)

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/cloud/test_ro_reader_nextcloud.py -v`
Expected: FAIL — `AttributeError: 'NextcloudBackend' object has no attribute 'ensure_ro_reader'`.

- [ ] **Step 4: Implement the Nextcloud reader trio**

In `orchestrator/services/cloud/nextcloud.py`, add `_READ_PERMISSION = 1` near the permission constants, then implement (using OCS admin endpoints — `secrets.token_urlsafe` for passwords):

```python
    async def ensure_ro_reader(self, *, user_key: str) -> str:
        """Idempotently ensure a dedicated low-privilege reader account. Creates
        no group memberships, so the account has zero folder access until a
        grant is minted. Tolerates OCS 102 (user already exists)."""
        self._ensure_ready()
        reader_id = f"srw-reader-{user_key}"
        resp = await self._client.post(
            "/ocs/v2.php/cloud/users",
            params={"format": "json"},
            data={"userid": reader_id, "password": secrets.token_urlsafe(24)},
        )
        # 100 = created, 102 = already exists — both fine; anything else is an error.
        self._require_ocs_status(resp, ok={100, 102}, op="create reader")
        return reader_id

    async def mint_ro_grant(
        self, handle: ProjectFolderHandle, *, user_key: str, grant_key: str
    ) -> RoReaderGrant:
        self._ensure_ready()
        reader_id = f"srw-reader-{user_key}"
        folder_id = handle.native_id
        mountpoint = handle.vendor_meta.get("mountpoint")
        group_id = f"srw-rog-{grant_key[:16]}"
        await self.ensure_group(group_id)
        # READ ONLY (permission=1) — never _ALL_PERMISSIONS.
        await self._grant_group_access(folder_id, group_id, self._READ_PERMISSION)
        await self.add_user_to_group(reader_id, group_id)
        # Rotate the reader credential per provision.
        app_password = secrets.token_urlsafe(24)
        pw_resp = await self._client.put(
            f"/ocs/v2.php/cloud/users/{reader_id}",
            params={"format": "json"},
            data={"key": "password", "value": app_password},
        )
        self._require_ocs_status(pw_resp, ok={100}, op="rotate reader password")
        webdav_url = (
            f"{self._base_url}/remote.php/dav/files/{reader_id}/"
            f"{quote(mountpoint, safe='')}/"
        )
        grant_handle = json.dumps(
            {"group_id": group_id, "folder_id": folder_id, "reader_id": reader_id}
        )
        return RoReaderGrant(
            reader_id=reader_id,
            grant_handle=grant_handle,
            webdav_url=webdav_url,
            credentials=app_password,
            auth_kind="basic",
        )

    async def revoke_ro_grant(self, grant_handle: str, *, user_key: str) -> None:
        self._ensure_ready()
        data = json.loads(grant_handle)
        reader_id = data["reader_id"]
        group_id = data["group_id"]
        # Removing the reader from the group AND deleting the group both drop
        # the folder access; deleting the group also removes the folder ACL row.
        try:
            await self.remove_user_from_group(reader_id, group_id)
        except CloudBackendError:
            pass  # already gone — revoke is idempotent
        await self._delete_group(group_id)
```

Add `_require_ocs_status` (parse the OCS `meta.statuscode` from the JSON body, raise `CloudBackendError` if not in `ok`) and `_delete_group` (`DELETE /ocs/v2.php/cloud/groups/{group_id}`) helpers near `ensure_group`. Add `import secrets`, `import json` if not present, and import `RoReaderGrant`, `CanaryFixture` from `.base`.

- [ ] **Step 5: Implement the canary fixture (Nextcloud)**

Add `seed_canary_fixture` / `remove_canary_fixture`. Seed writes a real file with the WRITE identity (`put_project_folder_file_bytes`) so the probe's side channels target a **real** path instead of synthetic ids (design §11.4). Version/trash id discovery is Nextcloud-version-specific; capture what is deterministically available and leave the rest `None` (the probe treats a `None` ref as "skip that side channel" — which under the strict gate keeps it fail-closed until live tuning):

```python
    async def seed_canary_fixture(self, handle: ProjectFolderHandle) -> CanaryFixture:
        path = ".srw-ro-canary/probe.txt"
        await self.put_project_folder_file_bytes(handle, path=path, content=b"canary")
        # version_ref/trash_ref require live NC version/trash APIs to enumerate
        # real ids; wired + tuned during the §11.4 live-validation step. Until
        # then they are None and those side channels stay inconclusive → refuse.
        return CanaryFixture(path=path, version_ref=None, trash_ref=None)

    async def remove_canary_fixture(
        self, handle: ProjectFolderHandle, fixture: CanaryFixture
    ) -> None:
        try:
            await self.delete_project_folder_file(handle, path=fixture.path, if_exists=True)
        except CloudBackendError:
            pass
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/cloud/test_ro_reader_nextcloud.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add orchestrator/services/cloud/base.py orchestrator/services/cloud/nextcloud.py tests/cloud/test_ro_reader_nextcloud.py
git commit -m "feat(cloud): Nextcloud RO-reader provisioning (per-user account, per-mount read grant)"
```

---

### Task A6: OpenCloud RO-reader provisioning

Implement the same `SupportsRoReader` interface for OpenCloud using the already-defined-but-unused **Viewer** role (`_SPACE_VIEWER_ROLE_NAME`/`_WEIGHT` = 40). OpenCloud is code-read-only/unverified-live (§11.7) and reader-account creation depends on KC admin (open question §9.2), so this task delivers the interface + unit tests against a fake; **live validation is deferred to the §11.4 manual gate** and noted as a risk.

**Files:**
- Modify: `orchestrator/services/cloud/opencloud.py` (the reader trio + canary)
- Test: `tests/cloud/test_ro_reader_opencloud.py` (create)

**Interfaces:**
- Consumes: `_SPACE_VIEWER_ROLE_NAME`/`_WEIGHT`, `_role_id`, `_graph_post`, `_graph_delete`, `ensure_user` (opencloud.py); `keycloak_admin.KeycloakGroupSync`-style admin for KC user create (or the existing OC user-create path if KC linkage suffices on dev).
- Produces: `OpenCloudBackend` implementations of the five `SupportsRoReader` methods. `mint_ro_grant` returns `RoReaderGrant(auth_kind="keycloak_user_impersonation", credentials=None, ...)`; the short-TTL bearer is minted at mount time by Slice B, not stored.

- [ ] **Step 1: Write the failing OpenCloud test**

Create `tests/cloud/test_ro_reader_opencloud.py`, modeling the fake on `tests/cloud/test_opencloud.py`'s existing transport fake. Assert: `ensure_ro_reader` creates/links `srw-reader-<key>`; `mint_ro_grant` invites that user to the drive with the **Viewer** role (weight 40, "Can view") and returns the permission id as `grant_handle`, `credentials=None`, `auth_kind="keycloak_user_impersonation"`; `revoke_ro_grant` DELETEs that permission id. Example assertion for the role:

```python
@pytest.mark.asyncio
async def test_mint_grant_uses_viewer_role_not_editor():
    backend, fake = _oc_backend_with_fake()
    await backend.ensure_ro_reader(user_key="abc")
    grant = await backend.mint_ro_grant(_oc_handle(), user_key="abc", grant_key="thread-1")
    invite = fake.invites[-1]
    assert invite["roles"] == [backend._role_id("Can view", 40)]  # Viewer, not "Can edit"
    assert grant.credentials is None
    assert grant.auth_kind == "keycloak_user_impersonation"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/cloud/test_ro_reader_opencloud.py -v`
Expected: FAIL — `AttributeError: 'OpenCloudBackend' object has no attribute 'ensure_ro_reader'`.

- [ ] **Step 3: Implement the OpenCloud reader trio + canary**

In `orchestrator/services/cloud/opencloud.py`, implement:
- `ensure_ro_reader(user_key)`: ensure a `srw-reader-<user_key>` identity exists. On dev (own KC) create the KC user via the `keycloak_admin` client then link the LibreGraph user via the existing `ensure_user` POST `/graph/v1.0/users` with `identities:[{issuer, issuerAssignedId: sub}]`. Return the LibreGraph user id. Add a module comment referencing §9.2 (prod shared-KC may lack user-create capability — the prod path is a documented open question, not solved here).
- `mint_ro_grant(handle, *, user_key, grant_key)`: POST `/graph/v1beta1/drives/{drive_id}/root/invite` inviting the reader user with `_role_id(_SPACE_VIEWER_ROLE_NAME, _SPACE_VIEWER_ROLE_WEIGHT)`; capture the returned permission id. Reconstruct the reader's Space WebDAV URL the same way `build_rclone_mount_spec` does (`/dav/spaces/{drive_id}/`). Return `RoReaderGrant(reader_id, grant_handle=json.dumps({"permission_id":..., "drive_id":..., "reader_sub":...}), webdav_url=..., credentials=None, auth_kind="keycloak_user_impersonation")`.
- `revoke_ro_grant(grant_handle, *, user_key)`: `_graph_delete(f"/graph/v1beta1/drives/{drive_id}/root/permissions/{permission_id}")`.
- `seed_canary_fixture` / `remove_canary_fixture`: same shape as Nextcloud (PUT a real canary file via the write identity; `version_ref`/`trash_ref = None` pending live tuning).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/cloud/test_ro_reader_opencloud.py -v`
Expected: PASS.

- [ ] **Step 5: Record the OpenCloud-live risk**

Append a bullet to `docs/design/cloud_access_unification.md` §9 question 2 (RO identity on prod's shared Keycloak): note that Slice A implements the OC reader via dev-KC user-create, and that prod-private's shared KC user-create capability + live Viewer-role RO verification remain the §11.4 blocker before protected mode may engage on OpenCloud.

- [ ] **Step 6: Commit**

```bash
git add orchestrator/services/cloud/opencloud.py tests/cloud/test_ro_reader_opencloud.py docs/design/cloud_access_unification.md
git commit -m "feat(cloud): OpenCloud RO-reader provisioning via Space Viewer role (§9.2 live-validation deferred)"
```

---

### Task A7: RO engage gate (`ro_engage.py`) — wire the probe + canary

The fail-closed capstone: given a user + project-folder handle + backend, provision the reader, mint the grant, seed a canary, run `ro_probe` (positive read control → mutating verbs → side channels) **as the reader**, verify version floors, and only persist the `cloud_ro_mounts` row + return the grant if `RoProbeResult.ok`; otherwise revoke the grant and refuse. This is the first caller of `ro_probe.py` (which shipped in Phase 0 with 19 tests but is not yet wired anywhere).

**Files:**
- Create: `orchestrator/services/cloud/ro_engage.py`
- Test: `tests/cloud/test_ro_engage.py` (create)

**Interfaces:**
- Consumes: `ro_probe.probe_read_only`, `ro_probe.check_version_floors` (orchestrator/services/cloud/ro_probe.py); `SupportsRoReader` methods (A5/A6); `PostgresDB.create_ro_mount` (A4); `_is_protected_cloud_mode_enabled` semantics (the caller in Slice B checks the flag; the gate itself is pure).
- Produces:
  - `ro_engage.RoEngageRefused(Exception)` — raised when the gate refuses (probe not ok / version below floor / flag off).
  - `ro_engage.engage_ro_mount(*, backend, handle, user_key, thread_id, user_id, postgres_db, http_client_factory) -> RoReaderGrant` — the orchestration. On success returns the grant AND has persisted the `cloud_ro_mounts` row. On any failure raises `RoEngageRefused` after best-effort revoke + canary cleanup.

- [ ] **Step 1: Write the failing test**

Create `tests/cloud/test_ro_engage.py`. Use a fake backend implementing `SupportsRoReader` + a fake httpx client the reader probe hits, and an `AsyncMock` postgres. Cover the three core paths:

```python
@pytest.mark.asyncio
async def test_engage_persists_and_returns_grant_when_probe_ok():
    backend = _FakeRoBackend()           # mint returns a grant; canary seeds fine
    probe_client = _reader_client(all_rejected=True, read_control=207)
    db = AsyncMock()
    db.create_ro_mount = AsyncMock(return_value="row-1")
    grant = await engage_ro_mount(
        backend=backend, handle=_handle(), user_key="abc",
        thread_id="t1", user_id="u1", postgres_db=db,
        http_client_factory=lambda creds: probe_client,
    )
    assert grant.reader_id == "srw-reader-abc"
    db.create_ro_mount.assert_awaited_once()
    assert backend.revoked == []          # not revoked on success


@pytest.mark.asyncio
async def test_engage_refuses_and_revokes_when_a_write_succeeds():
    backend = _FakeRoBackend()
    probe_client = _reader_client(all_rejected=False, read_control=207)  # a PUT 201s
    db = AsyncMock()
    with pytest.raises(RoEngageRefused):
        await engage_ro_mount(
            backend=backend, handle=_handle(), user_key="abc",
            thread_id="t1", user_id="u1", postgres_db=db,
            http_client_factory=lambda creds: probe_client,
        )
    db.create_ro_mount.assert_not_awaited()
    assert backend.revoked  # grant was rolled back


@pytest.mark.asyncio
async def test_engage_refuses_when_version_below_floor():
    backend = _FakeRoBackend()
    probe_client = _reader_client(all_rejected=True, read_control=207, nc_version="27.0.0")
    db = AsyncMock()
    with pytest.raises(RoEngageRefused):
        await engage_ro_mount(
            backend=backend, handle=_handle(), user_key="abc",
            thread_id="t1", user_id="u1", postgres_db=db,
            http_client_factory=lambda creds: probe_client,
        )
    assert backend.revoked
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/cloud/test_ro_engage.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'orchestrator.services.cloud.ro_engage'`.

- [ ] **Step 3: Write the engage gate**

Create `orchestrator/services/cloud/ro_engage.py`:

```python
"""Fail-closed RO engage gate for protected cloud mode (design §3.3, §11.4).

Provisions the per-user reader + per-mount grant, then verifies — as the
reader identity — that the mount is genuinely read-only before it may be used:
version floors (CVE side-channel patches) AND a live probe of every mutating
verb, with a positive read control so a dead credential cannot pass by 401ing
everywhere. On ANY failure it revokes the grant and raises RoEngageRefused;
only an ``ok`` probe persists the cloud_ro_mounts row.
"""
from __future__ import annotations

import logging

from . import ro_probe
from .base import RoReaderGrant

logger = logging.getLogger(__name__)


class RoEngageRefused(Exception):
    """The RO gate refused to engage — protected mode must NOT mount."""


async def engage_ro_mount(
    *,
    backend,
    handle,
    user_key: str,
    thread_id: str,
    user_id: str,
    postgres_db,
    http_client_factory,
) -> RoReaderGrant:
    await backend.ensure_ro_reader(user_key=user_key)
    grant = await backend.mint_ro_grant(handle, user_key=user_key, grant_key=thread_id)
    canary = None
    try:
        canary = await backend.seed_canary_fixture(handle)
        # Probe AS THE READER using its freshly minted credential.
        client = http_client_factory(grant.credentials)
        floors = await ro_probe.check_version_floors(
            client, grant.webdav_url, backend=backend.backend_id
        )
        if not floors.ok:
            raise RoEngageRefused(
                f"version floor check failed: {floors.failures or floors.inconclusive}"
            )
        # probe_read_only(client, base_url, path, *, dav_root=None, username=None)
        # — `path` is the positional target (the canary file); the reader's
        # WebDAV URL is both base_url and dav_root (ro_probe.py:240).
        result = await ro_probe.probe_read_only(
            client,
            grant.webdav_url,
            canary.path,
            dav_root=grant.webdav_url,
            username=grant.reader_id,
        )
        if not result.ok:
            raise RoEngageRefused(
                "read-only probe did not pass: "
                f"failures={result.failures} inconclusive={result.inconclusive} "
                f"skipped={result.skipped}"
            )
        row_id = await postgres_db.create_ro_mount(
            thread_id=thread_id,
            user_id=user_id,
            backend=backend.backend_id,
            reader_id=grant.reader_id,
            grant_handle=grant.grant_handle,
            credentials=grant.credentials,
            webdav_url=grant.webdav_url,
            auth_kind=grant.auth_kind,
        )
        logger.info("RO mount engaged for thread %s (row %s)", thread_id, row_id)
        return grant
    except Exception:
        # Fail closed: roll the grant back so no partial RO access lingers.
        try:
            await backend.revoke_ro_grant(grant.grant_handle, user_key=user_key)
        except Exception:  # pragma: no cover - best effort
            logger.exception("failed to revoke RO grant during engage rollback")
        raise
    finally:
        if canary is not None:
            try:
                await backend.remove_canary_fixture(handle, canary)
            except Exception:  # pragma: no cover
                logger.exception("failed to remove canary fixture")
```

Note: check `probe_read_only`'s real parameter name for the target path (recon shows `path` positional + `dav_root`/`username` kwargs — align the call to the actual signature in `ro_probe.py:240`; adjust `handle_path=` accordingly). Wrap `RoEngageRefused` so it is NOT swallowed by the `except Exception` rollback re-raise (it re-raises as-is, which is correct — the test asserts `RoEngageRefused` propagates).

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/cloud/test_ro_engage.py -v`
Expected: PASS (all three paths).

- [ ] **Step 5: Add the live-validation note**

In `docs/design/cloud_access_unification.md` §11.4, change the status from "⏳ OPEN — NOT RUN" wording to note that the **module is now wired** (`ro_engage.py`) with unit coverage, and that the remaining open item is narrowed to the **live status-code tuning** of the canary side channels against real NC ≥28.0.3 and OpenCloud (the `version_ref`/`trash_ref` discovery) — a manual step, still gating engage-on-real-backends.

- [ ] **Step 6: Commit**

```bash
git add orchestrator/services/cloud/ro_engage.py tests/cloud/test_ro_engage.py docs/design/cloud_access_unification.md
git commit -m "feat(cloud): fail-closed RO engage gate wiring ro_probe + canary (§11.4)"
```

---

### Task A8: Orphaned-reader reconciler sweep

Never trust revoke-on-teardown alone (design §8.1.4, [[srw_agent_headscale_ephemeral_leak]] lesson). A leader-gated periodic loop revokes `cloud_ro_mounts` grants whose thread no longer exists (or is ended).

**Files:**
- Create: `orchestrator/services/ro_reader_reconciler.py`
- Modify: `orchestrator/main.py` (register the loop alongside other `run_when_leader` loops, ~:5667 region)
- Test: `tests/test_ro_reader_reconciler.py` (create)

**Interfaces:**
- Consumes: `PostgresDB.list_active_ro_mounts`, `PostgresDB.get_thread`, `PostgresDB.mark_ro_mount_revoked` (A4); the backend router `main_cloud_router.for_backend(backend_id)` → `revoke_ro_grant`; `_is_protected_cloud_mode_enabled` (A1); `services.leader_election.is_leader`.
- Produces: `ro_reader_reconciler.reconcile_orphaned_ro_mounts(*, postgres_db, router) -> int` (returns count revoked — the pure, tested unit). The periodic leader-gated wrapper lives in `main.py` (it must close over the module-global `postgres_db`/`main_cloud_router` and take a `shutdown_event`, matching the existing sweeper idiom).

- [ ] **Step 1: Write the failing test**

Create `tests/test_ro_reader_reconciler.py`:

```python
@pytest.mark.asyncio
async def test_reconciler_revokes_grants_for_dead_threads():
    db = AsyncMock()
    db.list_active_ro_mounts = AsyncMock(return_value=[
        {"id": "r1", "thread_id": "alive", "backend": "nextcloud",
         "grant_handle": '{"reader_id":"srw-reader-a","group_id":"g1"}',
         "user_id": "u1"},
        {"id": "r2", "thread_id": "dead", "backend": "nextcloud",
         "grant_handle": '{"reader_id":"srw-reader-b","group_id":"g2"}',
         "user_id": "u2"},
    ])
    db.get_thread = AsyncMock(side_effect=lambda tid: {"id": tid, "status": "active"} if tid == "alive" else None)
    db.mark_ro_mount_revoked = AsyncMock(return_value=True)
    backend = AsyncMock()
    router = MagicMock()
    router.for_backend = MagicMock(return_value=backend)

    n = await reconcile_orphaned_ro_mounts(postgres_db=db, router=router)
    assert n == 1
    backend.revoke_ro_grant.assert_awaited_once()  # only the dead one
    db.mark_ro_mount_revoked.assert_awaited_once_with("r2")


@pytest.mark.asyncio
async def test_reconciler_revokes_grants_for_ended_threads():
    db = AsyncMock()
    db.list_active_ro_mounts = AsyncMock(return_value=[
        {"id": "r3", "thread_id": "ended", "backend": "nextcloud",
         "grant_handle": '{"reader_id":"x","group_id":"g"}', "user_id": "u"},
    ])
    db.get_thread = AsyncMock(return_value={"id": "ended", "status": "ended"})
    db.mark_ro_mount_revoked = AsyncMock(return_value=True)
    backend = AsyncMock()
    router = MagicMock(for_backend=MagicMock(return_value=backend))
    n = await reconcile_orphaned_ro_mounts(postgres_db=db, router=router)
    assert n == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_ro_reader_reconciler.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'orchestrator.services.ro_reader_reconciler'`.

- [ ] **Step 3: Write the reconciler**

Create `orchestrator/services/ro_reader_reconciler.py`:

```python
"""Reconcile orphaned protected-mode RO grants (design §8.1.4).

Revoke-on-teardown can be skipped by a crash, a killed pod, or a lost teardown
path — so a leader-gated sweep independently revokes any active cloud_ro_mounts
grant whose thread is gone or ended, and marks the row revoked. Modeled on the
existing periodic reconcilers (lifecycle/reconciler, project_loop_sweeper).
"""
from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

_DEAD_THREAD_STATUSES = {"ended", "deleted"}


async def reconcile_orphaned_ro_mounts(*, postgres_db, router) -> int:
    revoked = 0
    for row in await postgres_db.list_active_ro_mounts():
        thread = await postgres_db.get_thread(row["thread_id"])
        is_orphan = thread is None or thread.get("status") in _DEAD_THREAD_STATUSES
        if not is_orphan:
            continue
        try:
            data = json.loads(row["grant_handle"])
            user_key = _user_key_from_grant(data)
            backend = router.for_backend(row["backend"])
            await backend.revoke_ro_grant(row["grant_handle"], user_key=user_key)
            await postgres_db.mark_ro_mount_revoked(row["id"])
            revoked += 1
        except Exception:
            logger.exception("failed to revoke orphaned RO mount %s", row["id"])
    if revoked:
        logger.info("RO reader reconciler revoked %d orphaned grant(s)", revoked)
    return revoked


def _user_key_from_grant(data: dict) -> str:
    # reader_id is "srw-reader-<user_key>"; strip the prefix for revoke().
    reader_id = data.get("reader_id", "")
    return reader_id.removeprefix("srw-reader-")


```

(No loop wrapper in this module — the leader-gated periodic wrapper lives in `main.py` so it can close over the `postgres_db`/`main_cloud_router` module globals, matching the existing sweeper idiom. `asyncio`/`logging` imports above stay for the pure function and its logging.)

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_ro_reader_reconciler.py -v`
Expected: PASS.

- [ ] **Step 5: Register the loop, leader-gated + flag-gated**

The existing leader loops are module-level `async def <name>(shutdown_event: asyncio.Event) -> None:` coroutine functions with a `while not shutdown_event.is_set():` body, registered as `run_when_leader(<name>, _shutdown_event)` (see `ide_session_ttl_sweeper` at `orchestrator/main.py:832` and its registration at ~:5707). Follow that shape exactly.

First, add the loop wrapper as a module-level function in `orchestrator/main.py` (near the other sweeper defs), closing over the `postgres_db` / `main_cloud_router` module globals:

```python
async def ro_reader_reconciler_loop(shutdown_event: asyncio.Event) -> None:
    """Leader-gated periodic sweep of orphaned protected-mode RO grants."""
    from services.ro_reader_reconciler import reconcile_orphaned_ro_mounts

    logger.info("RO reader reconciler started")
    while not shutdown_event.is_set():
        try:
            await reconcile_orphaned_ro_mounts(
                postgres_db=postgres_db, router=main_cloud_router
            )
        except Exception:
            logger.exception("RO reader reconciler pass failed")
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=900)
        except asyncio.TimeoutError:
            pass  # 15-min interval; re-check leadership + shutdown each pass
```

Then, in the leadership-loop registration region (~:5667, alongside the other `run_when_leader(...)` calls), register it flag-gated:

```python
    if _is_protected_cloud_mode_enabled():
        run_when_leader(ro_reader_reconciler_loop, _shutdown_event)
```

(If a neighbouring sweeper uses a plain `await asyncio.sleep(...)` instead of the `wait_for(shutdown_event.wait())` graceful-sleep, match whichever idiom dominates that file — both are correct; the `wait_for` form shuts down faster.)

- [ ] **Step 6: Verify the orchestrator imports cleanly**

Run: `python -c "import orchestrator.main"`
Expected: no ImportError (the lazy `from services.ro_reader_reconciler import ...` is inside the guard, so this mainly checks the top-level edits parse).
Run: `pytest tests/test_ro_reader_reconciler.py tests/test_protected_cloud_flag.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add orchestrator/services/ro_reader_reconciler.py orchestrator/main.py tests/test_ro_reader_reconciler.py
git commit -m "feat(cloud): leader-gated reconciler sweeps orphaned RO reader grants (§8.1.4)"
```

---

### Slice A — final verification

- [ ] **Run the full affected suite**

Run: `pytest tests/cloud/ tests/test_cloud_ro_mounts_db.py tests/test_ro_reader_reconciler.py tests/test_protected_cloud_flag.py -q`
Expected: all pass. This is the CI-representative subset for this slice.

- [ ] **Run ruff (CI lints it)**

Run: `ruff check orchestrator/services/cloud/ orchestrator/services/ro_reader_reconciler.py tests/cloud/`
Expected: clean (or auto-fixable; the push workflow also runs ruff).

- [ ] **Confirm schema snapshot is in sync**

Run: `scripts/schema-snapshot.sh app && git diff --exit-code orchestrator/database/schema_current.sql`
Expected: no diff (snapshot already regenerated in A4). If it diffs, commit the regeneration.

---

## Slices B and C — master outline (plan in full after Slice A lands)

These are deliberately left as outlines: each should be written up in full no-placeholder detail **after** the previous slice lands, so the plan is against code that exists (matching the iterative build-and-see approach and the §11.6 amendment set). Each is independently shippable and testable.

### Slice B — Workspace mount stack (RO mount + capture overlay + refresh/heal + snapshot)

Delivers: a protected session actually mounts fuse-overlayfs over a read-only rclone lower, and staged writes survive pod churn. Builds on Slice A's `engage_ro_mount` (the mount uses the RO grant's `webdav_url`/credentials instead of the `agent-service` cloud config).

Anticipated tasks:
1. **RO lower mount from the grant.** Extend `_build_agent_cloud_mount` (`orchestrator/main.py:17339`) so a protected thread's mount payload carries `access: read_only` and the RO reader's `webdav_url` + auth (from `cloud_ro_mounts`) instead of `agent-service`. The mount-script generator already emits `--read-only` (`src/services/cloud_mount/__init__.py:552`), so this is payload wiring, not new mount code.
2. **Overlay mount over the rclone lower.** New agent-side component (sibling to `RcloneMountManager`) that, after the RO rclone mount is up, mounts fuse-overlayfs with `lowerdir=<rclone mount>`, `upperdir=/home/agent-host/.overlay/upper`, `workdir=/home/agent-host/.overlay/work`, merged at `workspace/cloud`. Runs on the pod via the same remote-shell path (`_run_remote_script`). Carries amendment #2 (added-dirs-as-opaque: apply/enumerate treat opaque "deletes" of never-in-lower dirs as no-ops) at the enumerate/apply boundary that consumes `src/services/cloud_overlay/whiteout.enumerate_diff`.
3. **Refresh op + ENOTCONN health monitor** (amendment #3). Build the missing health monitor (`cloud_mount/__init__.py` has only unmount scripts): an ENOTCONN read-probe loop; the refresh sequence = quiesce agent → plain `fusermount3 -u` overlay → `rclone rc vfs/refresh recursive=true` (never `vfs/forget`) → remount overlay; the heal sequence = overlay `-uz` first → remount rclone → remount overlay. Guard against dual-instance workdir sharing.
4. **Bulk-delete cost guards** (amendment #1). Size-aware guard / VFS warm-cache priming so `rm -rf` over a cold lower does not download bodies to whiteout them; compose with existing `cloud_mount/guardrails.py`.
5. **Snapshot placement + tar xattrs** (amendment #6). Ensure upperdir/workdir at `/home/agent-host/.overlay/{upper,work}` are inside `snapshot_service.py` scope while the merged mount + raw rclone lower are outside; add `--xattrs`/`--acls` to the capture tar (`snapshot_service.py:389`) and ensure restore lands on the non-overlayfs emptyDir.
6. **Upperdir quota + fail-writes-at-cap** (§9.9, chosen: fail writes + warn). Enforce an upperdir size cap so writes fail with ENOSPC before the emptyDir sizeLimit evicts the pod; surface the approaching-cap warning to the status snapshot.

### Slice C — Staging → review → apply + the toggle (session-facing)

Delivers: the per-session protected toggle, the auto-stage-at-turn-end → S3 push, the `DiffSource`-generalized review surface, apply/reject, and the agent-honesty prompt copy. Builds on Slices A + B.

Anticipated tasks:
1. **Per-session toggle (immutable at creation).** Add `protected_cloud` to `ThreadCreateRequest` (`orchestrator/main.py:16479`); persist in `threads.metadata.config_override` (or a typed column beside `main_cloud_backend`); branch the provisioning fork (~:16714) to call Slice A's `engage_ro_mount` + Slice B's overlay. Checkbox in `session-create.component.ts` (or the Workspace accordion). No mid-session flip in v1.
2. **`DiffSource` protocol + `UpperdirDiffSource`.** Extract the narrow seam Mode A already funnels through (`_diff_files_by_tree` → path/status list; `get_file_content(ref)` → old/new bytes; baseline identity) into a `DiffSource` protocol; implement `GiteaDiffSource` (today) and `UpperdirDiffSource` (reads `enumerate_diff` output + per-path bytes from the staged tar). Abstract the `projects/<slug>/` prefix assumption.
3. **Auto-stage at turn end → S3.** At each turn boundary (agent naturally quiesced) push the upperdir delta tar to the `srw-snapshots` (or a dedicated) bucket via the streaming path; orchestrator reads it for review/apply (design §8.1.1). Doubles as the §7 data-loss-window mitigation.
4. **Session diff-review surface.** Mirror the job diff endpoints (`/api/jobs/{id}/diff*`) as session endpoints backed by `UpperdirDiffSource`; reuse the `job-diff-review` Angular component (its `JobDiffFileEntry`/`JobDiffFile` types are already source-agnostic) behind a "cloud changes (N)" badge in the `persistent-chat` status bar + a mounted child panel (keep CSS out of the 60KB parent scss). Batched review, apply-anytime; per-file view, whole-diff apply (matches today's all-or-nothing accept/reject).
5. **Apply / reject over the overlay diff.** `apply_diff_to_cloud` generalized to pull bytes from `UpperdirDiffSource`; conflict gate runs `detect_external_mods` against the Slice A etag baseline (re-captured after each apply); added-dirs-as-opaque handled 404-tolerantly (amendment #2). Reject discards the upperdir.
6. **Agent honesty copy.** Update the `Workspace:` block in `config/prompts/systemprompt_interactive.txt:72-75` (+ the per-family variants) so a protected-mode agent describes cloud writes as staged-for-review, not saved live; inject conditionally via the resolved-prompts overlay (`config_resolver.py`) when the session is protected.

---

## Self-review notes (author)

- **Spec coverage (Slice A):** flag (A1) ↔ §11 rollout; `_propfind` fix (A2) ↔ amendment #4b / §11.5; etag baseline (A3) ↔ amendment #4a / §3.4; RO identity + grants (A4-A6) ↔ §3.3/§8.1.4; probe wiring + canary (A7) ↔ amendment #5 / §11.4; reconciler (A8) ↔ §8.1.4. Amendments #1/#2/#3/#6 are Slice B; the toggle/review/apply/honesty are Slice C — all carried in the outline.
- **Type consistency:** `RoReaderGrant`/`CanaryFixture`/`SupportsRoReader` are defined once in A5 (`base.py`) and consumed unchanged in A6/A7/A8; `create_ro_mount` field names in A4 match the `engage_ro_mount` call in A7; `grant_handle` is JSON carrying `reader_id` in both the NC minter (A5) and the reconciler's `_user_key_from_grant` (A8).
- **Pinned against real code (verified while writing):** `probe_read_only(client, base_url, path, *, dav_root=None, username=None)` — A7 calls `path` positionally (`ro_probe.py:240`). Leader loops are `async def f(shutdown_event: asyncio.Event)` with `while not shutdown_event.is_set():`, registered `run_when_leader(f, _shutdown_event)` (`main.py:832` / registrations ~:5676-5726) — A8's wrapper matches. `RoProbeResult.ok = not (failures or skipped or inconclusive)` — the strict gate A7 relies on.
- **Still verify-against-reality at implementation time:** (a) the `FakeNextcloud` fake-binding helper name in `test_nextcloud.py` (grep how the fake attaches to `backend._client`); (b) the migrate CLI entrypoint/flags in `orchestrator/database/migrate.py`; (c) the OpenCloud transport-fake shape in `test_opencloud.py` for A6. Each is flagged inline at its step.
