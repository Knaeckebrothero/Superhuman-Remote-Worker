# IDE Extension & Profile Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Make a user's code-server extensions (and the license/state that unlocks paid ones like Monokai Pro) follow them into every new workspace, completing the per-user IDE persistence that today only covers config files.

**Architecture:** Hybrid transport forced by network reality (workspace pods reach Open VSX but not MinIO). **Phase A** reinstalls extension *binaries* in-pod at boot from Open VSX by `id@version`, driven by a small manifest in `users.settings['ide']['extensions']`. **Phase B** carries what Open VSX can't — `globalStorage` (license/activation) and any non-Open-VSX extension's bytes — via the orchestrator using the existing snapshot S3 transport, with a sentinel handshake so paid themes are active on first paint. Phase A is independently shippable and delivers ~90% of the value; Phase B adds license fidelity.

**Tech Stack:** Python 3.12 (asyncio), pytest, code-server CLI (`--install-extension`, `--list-extensions`), Open VSX REST API, boto3/MinIO (S3), Kubernetes ConfigMaps, POSIX shell (workspace entrypoint), Helm.

**Spec:** `docs/superpowers/specs/2026-06-04-ide-extension-sync-design.md`

> **STATUS — SHIPPED & VERIFIED (2026-06-05).** Phases A and B are implemented, unit-tested (62 tests green), committed on `develop`, deployed to dev, and live-verified. Two deviations surfaced during execution and are reflected in the code (not in the task bodies below):
> - **B5/B6 entrypoint ordering fix (commit `0578e3fd`):** the sentinel wait deadlocked because the readiness probe is `tcpSocket:30022` (sshd) and the orchestrator only pushes state post-Ready. sshd now starts *before* the bounded wait (and anchors the container via `wait $SSHD_PID`), so the pod can reach Ready during the wait. Verified on workspace image `sha-ce443c0`: no timeout, globalStorage present before code-server's first paint.
> - **B3 bytes-folder resolution fix (commit `c052f7c7`):** capture tarred a bare `EXTENSIONS_DIR/{id}-{ver}` (Step 3 / line ~1264 below), but code-server names folders `{id}-{ver}-{platform}` (e.g. `-universal`). `capture_ide_profile` now calls `_resolve_ext_dir()` to find the real folder first. Deployed on orchestrator `sha-c052f7c`.

---

## File Structure

| File | Responsibility | Phase |
|---|---|---|
| `orchestrator/services/ide_settings.py` | New pure helpers: extension list script/parser, change-signature script/parser, Open VSX classifier, extension-install seed script, manifest store methods, globalStorage/bytes capture+seed over SSH | A + B |
| `orchestrator/services/ide_profile_store.py` (new) | S3 wrapper for `ide-profiles/<user_id>/...` blobs (globalStorage + per-extension bytes); reuses the snapshot boto3 client | B |
| `orchestrator/main.py` | Extend `code_server_settings_sweeper` to reconcile extensions (Phase A) and capture state when changed (Phase B) | A + B |
| `orchestrator/services/container_provisioner.py` | Fold the extension manifest into the seed ConfigMap; add `expect-state` flag + post-provision state stream | A + B |
| `docker/workspace-entrypoint.sh` | Run the extension install (carried in `seed.sh`); wait on the state sentinel before starting code-server | A + B |
| `orchestrator/services/nats_bridge.py`, `ide_session.py`, `workspace_suspension.py` | Extend existing VM-ready / restore / suspend hooks for extension + state seed/capture | A + B |
| `tests/test_ide_settings.py`, `tests/test_ide_profile_store.py` (new) | Unit tests mirroring the existing `FakeSettingsDB` style | A + B |

**Conventions to follow (from the existing module):** functions are import-light and side-effect-free where possible (`build_*`/`parse_*` are pure); SSH runs through an injectable `_runner` (`SshRunner`) so tests never shell out; the store read-modify-writes the whole `ide` subtree because `update_user_settings` is a shallow `||` merge; pull/seed/reconcile **never raise out of the loop**.

---

# PHASE A — Extension reinstall by ID (Open VSX)

### Task A1: Extension list — remote script + parser

**Files:**
- Modify: `orchestrator/services/ide_settings.py` (add constants near line 43-50; add functions after `parse_pull_output`, ~line 107)
- Test: `tests/test_ide_settings.py`

- [x] **Step 1: Write the failing test**

```python
# tests/test_ide_settings.py — add import and a new test class
from orchestrator.services.ide_settings import (
    build_extensions_list_script,
    parse_extensions_list,
)


class TestExtensionList:
    def test_parses_id_version_and_theme_flag(self):
        out = (
            "monokai.theme-monokai-pro-vscode@2.0.13\tTHEME\n"
            "ms-python.python@2024.4.1\t-\n"
            "garbage-line-no-tab\n"
        )
        result = parse_extensions_list(out)
        assert result == {
            "monokai.theme-monokai-pro-vscode": {"version": "2.0.13", "theme": True},
            "ms-python.python": {"version": "2024.4.1", "theme": False},
        }

    def test_empty_output_is_empty_dict(self):
        assert parse_extensions_list("") == {}

    def test_list_script_targets_extensions_dir(self):
        script = build_extensions_list_script()
        assert "/var/lib/code-server/extensions" in script
        assert "package.json" in script
```

- [x] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_ide_settings.py::TestExtensionList -v`
Expected: FAIL with `ImportError: cannot import name 'build_extensions_list_script'`

- [x] **Step 3: Write minimal implementation**

```python
# ide_settings.py — add near the other CODE_SERVER_* constants (~line 46)
EXTENSIONS_DIR = "/var/lib/code-server/extensions"
GLOBAL_STORAGE_DIR = f"{CODE_SERVER_USER_DIR}/globalStorage"
SEED_STATE_SENTINEL = "/var/lib/code-server/.ide-seed-state-done"

_EXT_THEME_FLAG = "THEME"


def build_extensions_list_script() -> str:
    """Remote shell: emit one ``<publisher>.<name>@<version>\\t<THEME|->`` line per
    installed extension. The theme flag is set when the extension's package.json
    declares a ``"themes"`` contribution, so the seed step can install theme
    providers first. Parses package.json with line-wise sed (top-level fields are
    one-per-line in published extensions); robust enough for ordering/inventory.
    """
    return (
        f"cd {EXTENSIONS_DIR} 2>/dev/null || exit 0\n"
        "for d in */ ; do\n"
        '  pj="${d%/}/package.json"\n'
        '  [ -f "$pj" ] || continue\n'
        "  pub=$(sed -n 's/.*\"publisher\"[: ]*\"\\([^\"]*\\)\".*/\\1/p' \"$pj\" | head -1)\n"
        "  nm=$(sed -n 's/.*\"name\"[: ]*\"\\([^\"]*\\)\".*/\\1/p' \"$pj\" | head -1)\n"
        "  ver=$(sed -n 's/.*\"version\"[: ]*\"\\([^\"]*\\)\".*/\\1/p' \"$pj\" | head -1)\n"
        '  [ -n "$pub" ] && [ -n "$nm" ] && [ -n "$ver" ] || continue\n'
        '  flag="-"\n'
        f'  grep -q \'"themes"\' "$pj" && flag="{_EXT_THEME_FLAG}"\n'
        '  printf \'%s.%s@%s\\t%s\\n\' "$pub" "$nm" "$ver" "$flag"\n'
        "done\n"
    )


def parse_extensions_list(stdout: str) -> dict[str, dict]:
    """Parse :func:`build_extensions_list_script` output into
    ``{ext_id: {"version": str, "theme": bool}}``. Lines without a tab are skipped.
    """
    result: dict[str, dict] = {}
    for line in stdout.split("\n"):
        if "\t" not in line:
            continue
        id_ver, _, flag = line.partition("\t")
        ext_id, _, version = id_ver.rpartition("@")
        if not ext_id or not version:
            continue
        result[ext_id] = {"version": version, "theme": flag.strip() == _EXT_THEME_FLAG}
    return result
```

- [x] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_ide_settings.py::TestExtensionList -v`
Expected: PASS (3 passed)

- [x] **Step 5: Commit**

```bash
git add orchestrator/services/ide_settings.py tests/test_ide_settings.py
git commit -m "feat(ide-ext): remote extension-list script + parser"
```

---

### Task A2: Open VSX classifier (openvsx vs bytes)

**Files:**
- Modify: `orchestrator/services/ide_settings.py` (add after `parse_extensions_list`)
- Test: `tests/test_ide_settings.py`

- [x] **Step 1: Write the failing test**

```python
from orchestrator.services.ide_settings import OpenVsxClassifier


class TestOpenVsxClassifier:
    @pytest.mark.asyncio
    async def test_available_returns_openvsx(self):
        calls = []

        async def fake_fetch(url):
            calls.append(url)
            return 200  # Open VSX has it

        clf = OpenVsxClassifier(fetch=fake_fetch)
        src = await clf.classify("monokai.theme-monokai-pro-vscode", "2.0.13")
        assert src == "openvsx"
        assert "monokai/theme-monokai-pro-vscode/2.0.13" in calls[0]

    @pytest.mark.asyncio
    async def test_missing_returns_bytes(self):
        async def fake_fetch(url):
            return 404

        clf = OpenVsxClassifier(fetch=fake_fetch)
        assert await clf.classify("acme.private", "1.0.0") == "bytes"

    @pytest.mark.asyncio
    async def test_result_is_cached(self):
        n = {"calls": 0}

        async def fake_fetch(url):
            n["calls"] += 1
            return 200

        clf = OpenVsxClassifier(fetch=fake_fetch)
        await clf.classify("a.b", "1.0.0")
        await clf.classify("a.b", "1.0.0")
        assert n["calls"] == 1  # cached by (id, version)

    @pytest.mark.asyncio
    async def test_fetch_error_defaults_to_bytes(self):
        async def boom(url):
            raise RuntimeError("network down")

        clf = OpenVsxClassifier(fetch=boom)
        assert await clf.classify("a.b", "1.0.0") == "bytes"
```

- [x] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_ide_settings.py::TestOpenVsxClassifier -v`
Expected: FAIL with `ImportError: cannot import name 'OpenVsxClassifier'`

- [x] **Step 3: Write minimal implementation**

```python
# ide_settings.py
OPEN_VSX_API = "https://open-vsx.org/api"

# Fetch signature: (url) -> http_status_int
VsxFetch = Callable[[str], Awaitable[int]]


async def _default_vsx_fetch(url: str) -> int:
    """HEAD/GET an Open VSX API URL; return the HTTP status. Runs urllib in a
    thread to stay dependency-light (no aiohttp import at module load)."""
    import urllib.error
    import urllib.request

    def _head() -> int:
        req = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                return resp.status
        except urllib.error.HTTPError as e:
            return e.code

    return await asyncio.to_thread(_head)


class OpenVsxClassifier:
    """Classify an extension as installable from Open VSX (``"openvsx"``) or
    requiring byte-copy (``"bytes"``). Caches by (id, version). On any error,
    defaults to ``"bytes"`` — the safe side (we'll carry the bytes ourselves)."""

    def __init__(self, fetch: Optional[VsxFetch] = None) -> None:
        self._fetch = fetch or _default_vsx_fetch
        self._cache: dict[tuple[str, str], str] = {}

    async def classify(self, ext_id: str, version: str) -> str:
        key = (ext_id, version)
        if key in self._cache:
            return self._cache[key]
        ns, _, name = ext_id.partition(".")
        url = f"{OPEN_VSX_API}/{ns}/{name}/{version}"
        try:
            status = await self._fetch(url)
            source = "openvsx" if status == 200 else "bytes"
        except Exception:  # noqa: BLE001 — classification must never raise
            source = "bytes"
        self._cache[key] = source
        return source
```

- [x] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_ide_settings.py::TestOpenVsxClassifier -v`
Expected: PASS (4 passed)

- [x] **Step 5: Commit**

```bash
git add orchestrator/services/ide_settings.py tests/test_ide_settings.py
git commit -m "feat(ide-ext): Open VSX availability classifier with cache"
```

---

### Task A3: Extension manifest store (get + merge)

**Files:**
- Modify: `orchestrator/services/ide_settings.py` (add methods to `IdeSettingsStore`, after `apply_pulled_files`)
- Test: `tests/test_ide_settings.py`

- [x] **Step 1: Write the failing test**

```python
class TestExtensionStore:
    @pytest.mark.asyncio
    async def test_apply_then_get_roundtrip(self):
        db = FakeSettingsDB()
        store = IdeSettingsStore(db)
        items = {
            "monokai.theme-monokai-pro-vscode": {
                "version": "2.0.13", "source": "openvsx", "theme": True
            }
        }
        changed = await store.apply_extensions(UID, items)
        assert changed == ["monokai.theme-monokai-pro-vscode"]
        got = await store.get_extensions(UID)
        assert got["monokai.theme-monokai-pro-vscode"]["version"] == "2.0.13"

    @pytest.mark.asyncio
    async def test_newer_version_wins_union_across_calls(self):
        db = FakeSettingsDB()
        store = IdeSettingsStore(db)
        await store.apply_extensions(UID, {"a.b": {"version": "1.0.0", "source": "openvsx", "theme": False}})
        # second workspace has a.b older + a new extension c.d
        await store.apply_extensions(UID, {
            "a.b": {"version": "0.9.0", "source": "openvsx", "theme": False},
            "c.d": {"version": "3.1.0", "source": "openvsx", "theme": False},
        })
        got = await store.get_extensions(UID)
        assert got["a.b"]["version"] == "1.0.0"   # newer kept (union, not clobber)
        assert got["c.d"]["version"] == "3.1.0"   # new one added

    @pytest.mark.asyncio
    async def test_apply_preserves_sibling_files_subtree(self):
        db = FakeSettingsDB({UID: {"ide": {"files": {"settings.json": _f("x", 1.0)}}}})
        store = IdeSettingsStore(db)
        await store.apply_extensions(UID, {"a.b": {"version": "1.0.0", "source": "openvsx", "theme": False}})
        files = await store.get_ide_files(UID)
        assert files["settings.json"] == _f("x", 1.0)  # not clobbered by shallow merge
```

- [x] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_ide_settings.py::TestExtensionStore -v`
Expected: FAIL with `AttributeError: 'IdeSettingsStore' object has no attribute 'apply_extensions'`

- [x] **Step 3: Write minimal implementation**

```python
# ide_settings.py — module-level helper
def _ver_key(v: str) -> tuple:
    """Sort key for version strings: numeric-aware, falls back to string parts.
    ``"2.0.13"`` > ``"2.0.9"``; non-numeric segments compare lexicographically."""
    parts = []
    for seg in str(v).replace("-", ".").split("."):
        parts.append((0, int(seg)) if seg.isdigit() else (1, seg))
    return tuple(parts)


# ide_settings.py — add to IdeSettingsStore
async def get_extensions(self, user_id: str) -> dict[str, dict]:
    """Return the stored extension manifest items: ``{id: {version, source, theme}}``."""
    settings = await self._db.get_user_settings(user_id)
    if not isinstance(settings, dict):
        return {}
    ide = settings.get("ide")
    exts = ide.get("extensions") if isinstance(ide, dict) else None
    items = exts.get("items") if isinstance(exts, dict) else None
    return dict(items) if isinstance(items, dict) else {}

async def apply_extensions(self, user_id: str, items: dict[str, dict]) -> list[str]:
    """Merge a workspace's installed extensions into the user's manifest.

    Union across workspaces, newest-version-wins per id (so an extension present
    only in workspace B survives a reconcile of workspace A). Returns the ids
    added or version-bumped. Read-modify-writes the whole ``ide`` subtree because
    ``update_user_settings`` is a shallow merge.
    """
    if not items:
        return []
    settings = await self._db.get_user_settings(user_id)
    if not isinstance(settings, dict):
        settings = {}
    ide = dict(settings.get("ide") or {}) if isinstance(settings.get("ide"), dict) else {}
    exts = dict(ide.get("extensions") or {}) if isinstance(ide.get("extensions"), dict) else {}
    stored = dict(exts.get("items") or {}) if isinstance(exts.get("items"), dict) else {}

    changed: list[str] = []
    for ext_id, entry in items.items():
        version = entry.get("version")
        if not version:
            continue
        prev = stored.get(ext_id)
        if prev is None or _ver_key(version) > _ver_key(prev.get("version", "")):
            stored[ext_id] = {
                "version": version,
                "source": entry.get("source", "bytes"),
                "theme": bool(entry.get("theme", False)),
            }
            changed.append(ext_id)

    if not changed:
        return []
    exts["items"] = stored
    ide["extensions"] = exts
    await self._db.update_user_settings(user_id, {"ide": ide})
    return changed
```

- [x] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_ide_settings.py::TestExtensionStore -v`
Expected: PASS (3 passed)

- [x] **Step 5: Commit**

```bash
git add orchestrator/services/ide_settings.py tests/test_ide_settings.py
git commit -m "feat(ide-ext): per-user extension manifest store (union, newest-version-wins)"
```

---

### Task A4: Extension-install seed script (theme-first)

**Files:**
- Modify: `orchestrator/services/ide_settings.py` (add after `build_seed_script`)
- Test: `tests/test_ide_settings.py`

- [x] **Step 1: Write the failing test**

```python
from orchestrator.services.ide_settings import build_extension_install_script


class TestExtensionInstallScript:
    def test_empty_is_noop(self):
        assert build_extension_install_script({}) == "exit 0\n"

    def test_only_openvsx_items_installed(self):
        items = {
            "monokai.theme-monokai-pro-vscode": {"version": "2.0.13", "source": "openvsx", "theme": True},
            "ms-python.python": {"version": "2024.4.1", "source": "openvsx", "theme": False},
            "acme.private": {"version": "1.0.0", "source": "bytes", "theme": False},
        }
        script = build_extension_install_script(items)
        assert "monokai.theme-monokai-pro-vscode@2.0.13" in script
        assert "ms-python.python@2024.4.1" in script
        assert "acme.private" not in script          # bytes source not installed here
        assert "--extensions-dir /var/lib/code-server/extensions" in script
        # theme installs synchronously before the backgrounded block
        theme_idx = script.index("monokai.theme-monokai-pro-vscode")
        bg_idx = script.index("ms-python.python")
        assert theme_idx < bg_idx
```

- [x] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_ide_settings.py::TestExtensionInstallScript -v`
Expected: FAIL with `ImportError: cannot import name 'build_extension_install_script'`

- [x] **Step 3: Write minimal implementation**

```python
# ide_settings.py
def build_extension_install_script(items: dict[str, dict]) -> str:
    """Shell that installs the user's Open-VSX extensions via the code-server CLI,
    run as ``agent-host``. Theme providers install **synchronously first** so the
    color theme is present when code-server first paints; the rest install in the
    background. Only ``source == "openvsx"`` items are handled here — ``bytes``
    items arrive via the orchestrator state stream (Phase B). Best-effort: a
    single failed install must not abort the rest (``|| true``)."""
    openvsx = {k: v for k, v in items.items() if v.get("source") == "openvsx"}
    if not openvsx:
        return "exit 0\n"

    def _install(ext_id: str, version: str) -> str:
        ref = _shq(f"{ext_id}@{version}")
        return (
            f"su -c 'code-server --install-extension {ref} "
            f"--extensions-dir {EXTENSIONS_DIR}' agent-host || true\n"
        )

    themes = [(k, v["version"]) for k, v in openvsx.items() if v.get("theme")]
    rest = [(k, v["version"]) for k, v in openvsx.items() if not v.get("theme")]

    parts = [f"mkdir -p {EXTENSIONS_DIR}\n"]
    for ext_id, version in themes:           # synchronous, theme-first
        parts.append(_install(ext_id, version))
    if rest:                                  # background the long tail
        parts.append("(\n")
        for ext_id, version in rest:
            parts.append(_install(ext_id, version))
        parts.append(f"chown -R agent-host:agent-host {EXTENSIONS_DIR}\n")
        parts.append(") &\n")
    parts.append(f"chown -R agent-host:agent-host {EXTENSIONS_DIR}\n")
    return "".join(parts)
```

- [x] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_ide_settings.py::TestExtensionInstallScript -v`
Expected: PASS (2 passed)

- [x] **Step 5: Commit**

```bash
git add orchestrator/services/ide_settings.py tests/test_ide_settings.py
git commit -m "feat(ide-ext): extension-install seed script (theme-first, openvsx only)"
```

---

### Task A5: Reconcile extensions in a pull cycle

**Files:**
- Modify: `orchestrator/services/ide_settings.py` (add `reconcile_extensions` after `reconcile_ide_settings`)
- Test: `tests/test_ide_settings.py`

- [x] **Step 1: Write the failing test**

```python
from orchestrator.services.ide_settings import reconcile_extensions


class TestReconcileExtensions:
    @pytest.mark.asyncio
    async def test_lists_classifies_and_stores(self):
        db = FakeSettingsDB()
        store = IdeSettingsStore(db)
        workspaces = [{"user_id": UID, "context": {"workspace_container": {"pod_ip": "10.0.0.1"}}}]

        async def list_fn(host, port):
            assert (host, port) == ("10.0.0.1", 30022)
            return {"a.b": {"version": "1.0.0", "theme": True}}

        class FakeClf:
            async def classify(self, ext_id, version):
                return "openvsx"

        n = await reconcile_extensions(store, workspaces, list_fn, FakeClf())
        assert n == 1
        got = await store.get_extensions(UID)
        assert got["a.b"] == {"version": "1.0.0", "source": "openvsx", "theme": True}

    @pytest.mark.asyncio
    async def test_unreachable_workspace_skipped(self):
        db = FakeSettingsDB()
        store = IdeSettingsStore(db)
        workspaces = [{"user_id": UID, "context": {"workspace_container": {"pod_ip": "10.0.0.1"}}}]

        async def list_fn(host, port):
            raise RuntimeError("ssh refused")

        class FakeClf:
            async def classify(self, ext_id, version):
                return "openvsx"

        assert await reconcile_extensions(store, workspaces, list_fn, FakeClf()) == 0
```

- [x] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_ide_settings.py::TestReconcileExtensions -v`
Expected: FAIL with `ImportError: cannot import name 'reconcile_extensions'`

- [x] **Step 3: Write minimal implementation**

```python
# ide_settings.py
# List-fn signature: (host, port) -> {id: {version, theme}}
ListFn = Callable[[str, int], Awaitable[dict]]


async def list_ide_extensions(
    ssh_host: str,
    ssh_port: int,
    *,
    key_path: Optional[str] = None,
    timeout: int = 20,
    _runner: Optional[SshRunner] = None,
) -> dict[str, dict]:
    """SSH into a workspace and return installed extensions. Never raises."""
    runner = _runner or _default_ssh_runner
    try:
        rc, stdout, _ = await runner(
            ssh_host, ssh_port, build_extensions_list_script(),
            key_path=key_path, timeout=timeout,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("ide_settings: ext-list failed for %s:%s — %s", ssh_host, ssh_port, e)
        return {}
    if rc != 0:
        return {}
    text = stdout.decode("utf-8", "replace") if isinstance(stdout, (bytes, bytearray)) else (stdout or "")
    return parse_extensions_list(text)


async def reconcile_extensions(
    store: "IdeSettingsStore",
    workspaces: list[dict],
    list_fn: ListFn,
    classifier: "OpenVsxClassifier",
) -> int:
    """For each workspace, list extensions, classify each (openvsx|bytes), and
    merge into the user's manifest. Returns the count of ids added/bumped.
    Order-independent and failure-isolated like ``reconcile_ide_settings``."""
    changed_total = 0
    for ws in workspaces:
        user_id = ws.get("user_id")
        if not user_id:
            continue
        target = resolve_ssh_target(_coerce_context(ws.get("context")))
        if not target:
            continue
        host, port = target
        try:
            listed = await list_fn(host, port)
        except Exception as e:  # noqa: BLE001
            logger.warning("ide_settings: ext reconcile list failed for %s:%s — %s", host, port, e)
            continue
        if not listed:
            continue
        items: dict[str, dict] = {}
        for ext_id, info in listed.items():
            source = await classifier.classify(ext_id, info.get("version", ""))
            items[ext_id] = {"version": info.get("version", ""), "source": source, "theme": bool(info.get("theme"))}
        try:
            changed = await store.apply_extensions(str(user_id), items)
        except Exception as e:  # noqa: BLE001
            logger.warning("ide_settings: ext reconcile apply failed for user %s — %s", user_id, e)
            continue
        changed_total += len(changed)
    return changed_total
```

- [x] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_ide_settings.py::TestReconcileExtensions -v`
Expected: PASS (2 passed)

- [x] **Step 5: Commit**

```bash
git add orchestrator/services/ide_settings.py tests/test_ide_settings.py
git commit -m "feat(ide-ext): list + reconcile extensions into the user manifest"
```

---

### Task A6: Sweeper reconciles extensions each cycle

**Files:**
- Modify: `orchestrator/main.py:728-760` (inside `code_server_settings_sweeper`)
- Test: manual (sweeper is glue; covered by A5 unit tests + live verification A10)

- [x] **Step 1: Extend the sweeper loop body**

In `code_server_settings_sweeper`, extend the import and the per-cycle work. Current import block (~line 748):

```python
    from services.ide_settings import (
        IdeSettingsStore,
        pull_ide_config,
        reconcile_ide_settings,
    )
```

Replace with:

```python
    from services.ide_settings import (
        IdeSettingsStore,
        OpenVsxClassifier,
        list_ide_extensions,
        pull_ide_config,
        reconcile_extensions,
        reconcile_ide_settings,
    )
```

After `store = IdeSettingsStore(postgres_db)` (~line 755) add:

```python
    classifier = OpenVsxClassifier()  # cache persists across cycles for this process
```

Inside the `while` loop, after the existing files reconcile (`count = await reconcile_ide_settings(...)`), add:

```python
            try:
                ext_changed = await reconcile_extensions(
                    store, workspaces, list_ide_extensions, classifier
                )
                if ext_changed:
                    logger.info("IDE settings sweeper: synced %d extension(s)", ext_changed)
            except Exception as e:  # noqa: BLE001
                logger.error("Error reconciling extensions: %s", e)
```

- [x] **Step 2: Verify it imports + compiles**

Run: `python -c "import ast,sys; ast.parse(open('orchestrator/main.py').read()); print('ok')"`
Expected: `ok`

Run: `ruff check orchestrator/main.py orchestrator/services/ide_settings.py`
Expected: `All checks passed!`

- [x] **Step 3: Commit**

```bash
git add orchestrator/main.py
git commit -m "feat(ide-ext): reconcile extensions in the code-server settings sweeper"
```

---

### Task A7: Fold the extension manifest into the seed ConfigMap

**Files:**
- Modify: `orchestrator/services/container_provisioner.py:686-733` (`_resolve_ide_seed_files`, `_create_seed_configmap`)
- Test: manual + live (A10). The script content is unit-tested in A4.

- [x] **Step 1: Resolve extensions alongside files**

`_resolve_ide_seed_files` (line 686) currently returns the user's `files` dict. Add a sibling resolver and have `_create_seed_configmap` compose both. Add a method next to `_resolve_ide_seed_files`:

```python
async def _resolve_ide_extensions(self, owner: "WorkspaceOwner") -> dict:
    """Return the owner's stored extension manifest items, or {} if none/no user."""
    user_id = getattr(owner, "user_id", None)
    if not user_id:
        return {}
    from services.ide_settings import IdeSettingsStore

    try:
        return await IdeSettingsStore(self._db).get_extensions(str(user_id))
    except Exception as e:  # noqa: BLE001 — seeding is best-effort
        logger.warning("ide seed: resolve extensions failed: %s", e)
        return {}
```

- [x] **Step 2: Compose `seed.sh` = files + extension installs**

In `_create_seed_configmap` (line 715), it currently builds `data={"seed.sh": build_seed_script(files)}`. Change the call sites (lines 189 and 494) that compute `seed_files` to also pass extensions, and update `_create_seed_configmap` to accept and append the install script.

Update the signature + body of `_create_seed_configmap`:

```python
async def _create_seed_configmap(
    self, pod_name: str, files: dict, extensions: Optional[dict] = None
) -> Optional[str]:
    # ... existing guard/setup ...
    from services.ide_settings import build_extension_install_script, build_seed_script

    seed_sh = build_seed_script(files)
    install_sh = build_extension_install_script(extensions or {})
    data = {"seed.sh": seed_sh + "\n" + install_sh}
    if extensions and any(v.get("source") == "bytes" for v in extensions.values()) or extensions and any(True for _ in [0]):
        pass  # placeholder removed below
    # ... existing create-or-replace logic, using `data` ...
```

Replace that placeholder block — the correct body sets `data` then creates the ConfigMap exactly as before:

```python
async def _create_seed_configmap(
    self, pod_name: str, files: dict, extensions: Optional[dict] = None
) -> Optional[str]:
    if not files and not extensions:
        return None
    from services.ide_settings import build_extension_install_script, build_seed_script

    cm_name = self._seed_configmap_name(pod_name)
    body = {
        "metadata": {"name": cm_name},
        "data": {"seed.sh": build_seed_script(files) + "\n" + build_extension_install_script(extensions or {})},
    }
    # ... unchanged create/replace-on-409 logic referencing `body` ...
    return cm_name
```

At the two call sites (lines ~189 and ~494), where `seed_files = await self._resolve_ide_seed_files(owner)` is followed by `seed_cm = await self._create_seed_configmap(pod_name, seed_files)`, insert:

```python
        seed_files = await self._resolve_ide_seed_files(owner)
        seed_exts = await self._resolve_ide_extensions(owner)
        seed_cm = await self._create_seed_configmap(pod_name, seed_files, seed_exts)
```

- [x] **Step 3: Verify compile + lint**

Run: `python -c "import ast; ast.parse(open('orchestrator/services/container_provisioner.py').read()); print('ok')"`
Run: `ruff check orchestrator/services/container_provisioner.py`
Expected: `ok` then `All checks passed!`

- [x] **Step 4: Commit**

```bash
git add orchestrator/services/container_provisioner.py
git commit -m "feat(ide-ext): carry extension-install manifest in the seed ConfigMap"
```

---

### Task A8: VM-ready + restore seed the extensions

**Files:**
- Modify: `orchestrator/services/ide_settings.py` (extend `seed_ide_config_for_user` to also install extensions over SSH)
- Modify call sites: `orchestrator/services/nats_bridge.py` (`_seed_vm_ide_config`), `orchestrator/services/ide_session.py` (`_restore_vm_session`) — no signature change needed if we extend the helper.
- Test: `tests/test_ide_settings.py`

- [x] **Step 1: Write the failing test**

```python
class TestSeedForUserInstallsExtensions:
    @pytest.mark.asyncio
    async def test_seeds_files_and_installs_openvsx_extensions(self):
        db = FakeSettingsDB({UID: {"ide": {
            "files": {"settings.json": _f('{"workbench.colorTheme":"Monokai Pro"}', 5.0)},
            "extensions": {"items": {"monokai.theme-monokai-pro-vscode": {"version": "2.0.13", "source": "openvsx", "theme": True}}},
        }}})
        scripts = []

        async def fake_runner(host, port, script, key_path=None, timeout=20):
            scripts.append(script)
            return 0, b"", b""

        ok = await seed_ide_config_for_user(db, UID, "10.0.0.5", 30022, _runner=fake_runner)
        assert ok is True
        joined = "\n".join(scripts)
        assert "settings.json" in joined                                   # files seeded
        assert "monokai.theme-monokai-pro-vscode@2.0.13" in joined         # extension installed
```

- [x] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_ide_settings.py::TestSeedForUserInstallsExtensions -v`
Expected: FAIL — the extension ref is not in the seeded script yet.

- [x] **Step 3: Extend `seed_ide_config_for_user`**

```python
async def seed_ide_config_for_user(
    db: Any,
    user_id: Optional[str],
    ssh_host: str,
    ssh_port: int,
    *,
    key_path: Optional[str] = None,
    _runner: Optional[SshRunner] = None,
) -> bool:
    if not user_id:
        return True
    store = IdeSettingsStore(db)
    files = await store.get_ide_files(str(user_id))
    extensions = await store.get_extensions(str(user_id))
    if not files and not extensions:
        return True
    runner = _runner or _default_ssh_runner
    script = build_seed_script(files) + "\n" + build_extension_install_script(extensions)
    try:
        rc, _out, stderr = await runner(ssh_host, ssh_port, script, key_path=key_path, timeout=60)
    except Exception as e:  # noqa: BLE001
        logger.warning("ide_settings: seed-for-user failed for %s:%s — %s", ssh_host, ssh_port, e)
        return False
    if rc != 0:
        err = stderr.decode("utf-8", "replace") if isinstance(stderr, (bytes, bytearray)) else (stderr or "")
        logger.warning("ide_settings: seed-for-user rc=%s for %s:%s — %s", rc, ssh_host, ssh_port, err[:200])
        return False
    return True
```

(The `nats_bridge._seed_vm_ide_config` and `ide_session._restore_vm_session` call sites already call `seed_ide_config_for_user`, so they gain extension install for free.)

- [x] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_ide_settings.py::TestSeedForUserInstallsExtensions -v`
Expected: PASS

- [x] **Step 5: Run the full module test + lint + commit**

Run: `pytest tests/test_ide_settings.py -v`
Expected: all pass (existing + new)
Run: `ruff check orchestrator/services/ide_settings.py`

```bash
git add orchestrator/services/ide_settings.py tests/test_ide_settings.py
git commit -m "feat(ide-ext): VM-ready/restore seed installs the user's extensions"
```

---

### Task A9: Phase A live verification (dev cluster)

**Files:** none (verification). Mirrors how the file-sync was verified.

- [x] **Step 1:** In an active session's IDE, install a **free Open VSX** extension (e.g. a theme like "Dracula Official") and set it as the color theme.
- [x] **Step 2:** Confirm capture — wait one sweep (≤10 min), then:

```bash
kubectl --context main -n superhuman-remote-worker exec srw-postgres-0 -- \
  psql -U srw -d srw -tAc "SELECT jsonb_pretty(settings->'ide'->'extensions') FROM users WHERE email='<user-email>';"
```
Expected: the extension id present with `"source": "openvsx"`.

- [x] **Step 3:** Provision a fresh session; once the pod is Running:

```bash
WS=<new ws-thread pod>; kubectl --context main -n superhuman-remote-worker exec $WS -- \
  sh -c 'ls /var/lib/code-server/extensions/; cat /mnt/code-server-config/seed.sh | grep install-extension'
```
Expected: the extension folder present; `--install-extension <id>@<ver>` in seed.sh.

- [x] **Step 4:** Open the new session's IDE; confirm the theme is applied on load.
- [x] **Step 5:** Commit nothing (verification only). If issues found, fix forward with new TDD tasks.

---

# PHASE B — globalStorage + non-Open-VSX bytes (MinIO) + sentinel

### Task B1: S3 profile store

**Files:**
- Create: `orchestrator/services/ide_profile_store.py`
- Test: `tests/test_ide_profile_store.py`

- [x] **Step 1: Write the failing test**

```python
# tests/test_ide_profile_store.py
import pytest
from orchestrator.services.ide_profile_store import IdeProfileStore

UID = "11111111-1111-1111-1111-111111111111"


class FakeS3:
    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body):
        self.objects[(Bucket, Key)] = Body.read() if hasattr(Body, "read") else Body

    def get_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            from botocore.exceptions import ClientError
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        body = self.objects[(Bucket, Key)]

        class _B:
            def read(self_inner):
                return body

        return {"Body": _B()}

    def head_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            from botocore.exceptions import ClientError
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
        return {}


def test_globalstorage_key_layout():
    store = IdeProfileStore(FakeS3(), "srw-snapshots")
    assert store.globalstorage_key(UID) == f"ide-profiles/{UID}/globalStorage.tar.zst"
    assert store.ext_bytes_key(UID, "a.b", "1.0.0") == f"ide-profiles/{UID}/ext/a.b/1.0.0.tar.zst"


@pytest.mark.asyncio
async def test_put_then_get_globalstorage_roundtrip(tmp_path):
    s3 = FakeS3()
    store = IdeProfileStore(s3, "srw-snapshots")
    src = tmp_path / "gs.tar.zst"; src.write_bytes(b"BLOB")
    await store.put_globalstorage(UID, str(src))
    dst = tmp_path / "out.tar.zst"
    ok = await store.get_globalstorage(UID, str(dst))
    assert ok and dst.read_bytes() == b"BLOB"


@pytest.mark.asyncio
async def test_get_missing_returns_false(tmp_path):
    store = IdeProfileStore(FakeS3(), "srw-snapshots")
    assert await store.get_globalstorage(UID, str(tmp_path / "x")) is False


@pytest.mark.asyncio
async def test_ext_bytes_exists(tmp_path):
    s3 = FakeS3(); store = IdeProfileStore(s3, "srw-snapshots")
    assert await store.ext_bytes_exists(UID, "a.b", "1.0.0") is False
    src = tmp_path / "e.tar.zst"; src.write_bytes(b"E")
    await store.put_ext_bytes(UID, "a.b", "1.0.0", str(src))
    assert await store.ext_bytes_exists(UID, "a.b", "1.0.0") is True
```

- [x] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_ide_profile_store.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'orchestrator.services.ide_profile_store'`

- [x] **Step 3: Write minimal implementation**

```python
# orchestrator/services/ide_profile_store.py
"""S3-backed store for per-user IDE *profile blobs* that don't fit JSONB:
the code-server ``globalStorage`` bundle (license/activation state) and the
bytes of any extension Open VSX can't provide. Layout::

    s3://<bucket>/ide-profiles/<user_id>/globalStorage.tar.zst
    s3://<bucket>/ide-profiles/<user_id>/ext/<id>/<version>.tar.zst

Reuses the snapshot boto3 client (injected) — no second client/credentials.
All blocking S3 calls run in a thread. Returns False rather than raising on a
missing object so seeding can degrade gracefully.
"""
import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

try:
    from botocore.exceptions import ClientError
except ImportError:  # boto3 optional in some envs
    ClientError = Exception  # type: ignore[misc,assignment]

_PREFIX = "ide-profiles"


class IdeProfileStore:
    def __init__(self, s3_client: Any, bucket: str) -> None:
        self._s3 = s3_client
        self._bucket = bucket

    def globalstorage_key(self, user_id: str) -> str:
        return f"{_PREFIX}/{user_id}/globalStorage.tar.zst"

    def ext_bytes_key(self, user_id: str, ext_id: str, version: str) -> str:
        return f"{_PREFIX}/{user_id}/ext/{ext_id}/{version}.tar.zst"

    async def put_globalstorage(self, user_id: str, local_path: str) -> None:
        await self._put(self.globalstorage_key(user_id), local_path)

    async def get_globalstorage(self, user_id: str, local_path: str) -> bool:
        return await self._get(self.globalstorage_key(user_id), local_path)

    async def put_ext_bytes(self, user_id: str, ext_id: str, version: str, local_path: str) -> None:
        await self._put(self.ext_bytes_key(user_id, ext_id, version), local_path)

    async def get_ext_bytes(self, user_id: str, ext_id: str, version: str, local_path: str) -> bool:
        return await self._get(self.ext_bytes_key(user_id, ext_id, version), local_path)

    async def ext_bytes_exists(self, user_id: str, ext_id: str, version: str) -> bool:
        def _head() -> bool:
            try:
                self._s3.head_object(Bucket=self._bucket, Key=self.ext_bytes_key(user_id, ext_id, version))
                return True
            except ClientError:
                return False

        return await asyncio.to_thread(_head)

    async def _put(self, key: str, local_path: str) -> None:
        def _do() -> None:
            with open(local_path, "rb") as f:
                self._s3.put_object(Bucket=self._bucket, Key=key, Body=f)

        await asyncio.to_thread(_do)

    async def _get(self, key: str, local_path: str) -> bool:
        def _do() -> bool:
            try:
                resp = self._s3.get_object(Bucket=self._bucket, Key=key)
            except ClientError:
                return False
            with open(local_path, "wb") as f:
                f.write(resp["Body"].read())
            return True

        return await asyncio.to_thread(_do)
```

- [x] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_ide_profile_store.py -v`
Expected: PASS (5 passed)

- [x] **Step 5: Commit**

```bash
git add orchestrator/services/ide_profile_store.py tests/test_ide_profile_store.py
git commit -m "feat(ide-ext): S3 profile store for globalStorage + extension bytes"
```

---

### Task B2: Change-signature script + parser

**Files:**
- Modify: `orchestrator/services/ide_settings.py`
- Test: `tests/test_ide_settings.py`

- [x] **Step 1: Write the failing test**

```python
from orchestrator.services.ide_settings import build_signature_script, parse_signature


class TestSignature:
    def test_script_covers_extensions_and_globalstorage(self):
        s = build_signature_script()
        assert "/var/lib/code-server/extensions" in s
        assert "/var/lib/code-server/User/globalStorage" in s
        assert "sha256sum" in s

    def test_parse_takes_first_token(self):
        assert parse_signature("abc123  -\n") == "abc123"
        assert parse_signature("") == ""
```

- [x] **Step 2: Run** `pytest tests/test_ide_settings.py::TestSignature -v` — FAIL (ImportError).

- [x] **Step 3: Implement**

```python
# ide_settings.py
def build_signature_script() -> str:
    """Remote shell: a cheap content signature over the extensions dir and
    globalStorage (paths + sizes + mtimes), hashed. Used to skip byte-copy when
    nothing changed. ``find -printf`` is GNU; falls back to ``ls -laR`` if absent."""
    targets = f"{EXTENSIONS_DIR} {GLOBAL_STORAGE_DIR}"
    return (
        f"if find {targets} -maxdepth 0 >/dev/null 2>&1; then\n"
        f"  (find {targets} -printf '%p %s %T@\\n' 2>/dev/null "
        f"   || ls -laR {targets} 2>/dev/null) | sort | sha256sum\n"
        "else echo ''; fi\n"
    )


def parse_signature(stdout: str) -> str:
    return stdout.strip().split()[0] if stdout.strip() else ""
```

- [x] **Step 4: Run** `pytest tests/test_ide_settings.py::TestSignature -v` — PASS.
- [x] **Step 5: Commit**

```bash
git add orchestrator/services/ide_settings.py tests/test_ide_settings.py
git commit -m "feat(ide-ext): cheap change-signature for extensions+globalStorage"
```

---

### Task B3: Capture globalStorage + bytes over SSH → S3

**Files:**
- Modify: `orchestrator/services/ide_settings.py` (add `capture_ide_profile`)
- Test: `tests/test_ide_settings.py` (mock runner + fake profile store + monkeypatched subprocess)

- [x] **Step 1: Write the failing test**

```python
class TestCaptureProfile:
    @pytest.mark.asyncio
    async def test_skips_when_signature_unchanged(self):
        db = FakeSettingsDB({UID: {"ide": {"extensions": {"sig": "SAME"}}}})
        store = IdeSettingsStore(db)

        async def sig_runner(host, port, script, key_path=None, timeout=20):
            return 0, b"SAME  -\n", b""

        captured = {"called": False}

        class FakeProfileStore:
            async def put_globalstorage(self, *a, **k):
                captured["called"] = True

        n = await capture_ide_profile(
            store, UID, "10.0.0.1", 30022, FakeProfileStore(),
            _runner=sig_runner, _tar_fn=None,
        )
        assert n == 0 and captured["called"] is False
```

(Full capture wiring — SSH-tar of `globalStorage`/bytes via a `_tar_fn` injection — is exercised live in B8; the unit test pins the **signature-skip** fast path, which is the costly-path guard.)

- [x] **Step 2: Run** `pytest tests/test_ide_settings.py::TestCaptureProfile -v` — FAIL (ImportError).

- [x] **Step 3: Implement**

```python
# ide_settings.py — add to IdeSettingsStore: signature get/set helpers
async def get_ext_signature(self, user_id: str) -> str:
    settings = await self._db.get_user_settings(user_id)
    ide = settings.get("ide") if isinstance(settings, dict) else None
    exts = ide.get("extensions") if isinstance(ide, dict) else None
    return exts.get("sig", "") if isinstance(exts, dict) else ""

async def set_ext_signature(self, user_id: str, sig: str) -> None:
    settings = await self._db.get_user_settings(user_id)
    if not isinstance(settings, dict):
        settings = {}
    ide = dict(settings.get("ide") or {}) if isinstance(settings.get("ide"), dict) else {}
    exts = dict(ide.get("extensions") or {}) if isinstance(ide.get("extensions"), dict) else {}
    exts["sig"] = sig
    ide["extensions"] = exts
    await self._db.update_user_settings(user_id, {"ide": ide})


# ide_settings.py — module-level
TarFn = Callable[..., Awaitable[bool]]  # (host, port, remote_dir, local_path) -> ok


async def _ssh_tar_to_file(
    ssh_host: str, ssh_port: int, remote_path: str, local_path: str,
    *, key_path: Optional[str] = None, timeout: int = 120,
) -> bool:
    """Stream ``ssh agent-host@host 'tar -cf - <remote_path> | zstd' > local`` —
    the snapshot_service transport, narrowed to one path. Returns False on error."""
    from services import resolve_ssh_key_path

    kp = key_path if key_path is not None else resolve_ssh_key_path()
    remote = f"tar -cf - {remote_path} 2>/dev/null | zstd -1 -T0"
    cmd = [
        "ssh", *(["-i", kp] if kp else []),
        "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ConnectTimeout=10", "-p", str(ssh_port),
        f"agent-host@{ssh_host}", remote,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    total = 0
    try:
        with open(local_path, "wb") as f:
            while True:
                chunk = await asyncio.wait_for(proc.stdout.read(1 << 20), timeout=timeout)
                if not chunk:
                    break
                total += len(chunk)
                f.write(chunk)
        await proc.wait()
    except Exception as e:  # noqa: BLE001
        logger.warning("ide_settings: tar capture failed %s:%s — %s", ssh_host, ssh_port, e)
        return False
    return proc.returncode == 0 and total > 0


async def capture_ide_profile(
    store: "IdeSettingsStore",
    user_id: str,
    ssh_host: str,
    ssh_port: int,
    profile_store: Any,
    *,
    key_path: Optional[str] = None,
    _runner: Optional[SshRunner] = None,
    _tar_fn: Optional[TarFn] = None,
) -> int:
    """If the workspace's extensions/globalStorage changed since last capture,
    tar globalStorage (and any ``bytes`` extension's folder) to the S3 profile
    store and record the new signature. Returns the number of blobs uploaded.
    Never raises."""
    import tempfile

    runner = _runner or _default_ssh_runner
    tar_fn = _tar_fn or _ssh_tar_to_file
    try:
        rc, out, _ = await runner(ssh_host, ssh_port, build_signature_script(), key_path=key_path, timeout=30)
    except Exception:  # noqa: BLE001
        return 0
    if rc != 0:
        return 0
    sig = parse_signature(out.decode("utf-8", "replace") if isinstance(out, (bytes, bytearray)) else (out or ""))
    if not sig or sig == await store.get_ext_signature(user_id):
        return 0

    uploaded = 0
    # globalStorage bundle
    with tempfile.NamedTemporaryFile(suffix=".tar.zst", delete=True) as tmp:
        if tar_fn and await tar_fn(ssh_host, ssh_port, GLOBAL_STORAGE_DIR, tmp.name, key_path=key_path):
            await profile_store.put_globalstorage(user_id, tmp.name)
            uploaded += 1
    # bytes extensions (only those classified bytes + not already stored)
    items = await store.get_extensions(user_id)
    for ext_id, info in items.items():
        if info.get("source") != "bytes":
            continue
        version = info.get("version", "")
        if await profile_store.ext_bytes_exists(user_id, ext_id, version):
            continue
        with tempfile.NamedTemporaryFile(suffix=".tar.zst", delete=True) as tmp:
            remote = f"{EXTENSIONS_DIR}/{ext_id}-{version}"
            if tar_fn and await tar_fn(ssh_host, ssh_port, remote, tmp.name, key_path=key_path):
                await profile_store.put_ext_bytes(user_id, ext_id, version, tmp.name)
                uploaded += 1

    await store.set_ext_signature(user_id, sig)
    return uploaded
```

- [x] **Step 4: Run** `pytest tests/test_ide_settings.py::TestCaptureProfile -v` — PASS.
- [x] **Step 5: Commit**

```bash
git add orchestrator/services/ide_settings.py tests/test_ide_settings.py
git commit -m "feat(ide-ext): capture globalStorage + bytes extensions to S3 (signature-gated)"
```

---

### Task B4: Seed state over SSH + sentinel touch

**Files:**
- Modify: `orchestrator/services/ide_settings.py` (add `seed_ide_profile`)
- Test: `tests/test_ide_settings.py`

- [x] **Step 1: Write the failing test**

```python
class TestSeedProfile:
    @pytest.mark.asyncio
    async def test_extracts_globalstorage_and_touches_sentinel(self, tmp_path):
        # profile store yields a blob; runner records the extract+sentinel script
        scripts = []

        class FakeProfileStore:
            async def get_globalstorage(self, uid, path):
                open(path, "wb").write(b"GS"); return True
            async def get_ext_bytes(self, *a, **k):
                return False

        async def fake_runner(host, port, script, key_path=None, timeout=20):
            scripts.append(script); return 0, b"", b""

        from orchestrator.services.ide_settings import seed_ide_profile, SEED_STATE_SENTINEL
        ok = await seed_ide_profile(
            user_id=UID, ssh_host="h", ssh_port=30022, profile_store=FakeProfileStore(),
            ext_items={}, _runner=fake_runner, _push_fn=lambda *a, **k: _ok(),
        )
        assert ok is True
        assert any(SEED_STATE_SENTINEL in s for s in scripts)
```

> NOTE for implementer: the test above needs a small async `_ok()` helper returning True and a `_push_fn` injection point that mirrors `_tar_fn` (pushes a local tar into the pod via `ssh ... 'zstd -d | tar -xf - -C /'`). Define `_push_fn` default `_ssh_untar_from_file` analogous to `_ssh_tar_to_file`. Write the helper test-first the same way (a runner-injected push) — keep the public `seed_ide_profile` signature stable.

- [x] **Step 2: Run** the test — FAIL (ImportError).

- [x] **Step 3: Implement** `_ssh_untar_from_file` (reverse of `_ssh_tar_to_file`: `cat local | ssh ... 'zstd -d | tar -xf - -C /'`) and:

```python
async def seed_ide_profile(
    *,
    user_id: str,
    ssh_host: str,
    ssh_port: int,
    profile_store: Any,
    ext_items: dict,
    key_path: Optional[str] = None,
    _runner: Optional[SshRunner] = None,
    _push_fn: Optional[Any] = None,
) -> bool:
    """Restore globalStorage (+ any bytes extensions) into a workspace, then touch
    the sentinel the entrypoint waits on. Best-effort; returns True if the sentinel
    was written. Never raises."""
    import tempfile

    runner = _runner or _default_ssh_runner
    push = _push_fn or _ssh_untar_from_file
    try:
        with tempfile.NamedTemporaryFile(suffix=".tar.zst", delete=True) as tmp:
            if await profile_store.get_globalstorage(user_id, tmp.name):
                await push(ssh_host, ssh_port, tmp.name, key_path=key_path)
        for ext_id, info in (ext_items or {}).items():
            if info.get("source") != "bytes":
                continue
            with tempfile.NamedTemporaryFile(suffix=".tar.zst", delete=True) as tmp:
                if await profile_store.get_ext_bytes(user_id, ext_id, info.get("version", ""), tmp.name):
                    await push(ssh_host, ssh_port, tmp.name, key_path=key_path)
        # chown + sentinel
        rc, _o, _e = await runner(
            ssh_host, ssh_port,
            f"chown -R agent-host:agent-host {CODE_SERVER_USER_DIR} {EXTENSIONS_DIR} 2>/dev/null; "
            f"touch {SEED_STATE_SENTINEL}\n",
            key_path=key_path, timeout=30,
        )
        return rc == 0
    except Exception as e:  # noqa: BLE001
        logger.warning("ide_settings: profile seed failed %s:%s — %s", ssh_host, ssh_port, e)
        return False
```

- [x] **Step 4: Run** the test — PASS.
- [x] **Step 5: Commit**

```bash
git add orchestrator/services/ide_settings.py tests/test_ide_settings.py
git commit -m "feat(ide-ext): restore globalStorage/bytes into a workspace + sentinel"
```

---

### Task B5: Entrypoint waits for the state sentinel

**Files:**
- Modify: `docker/workspace-entrypoint.sh` (between section 2b at line 49 and code-server start at line 56)

- [x] **Step 1: Add the gated wait**

After the `seed.sh` block (line 49), insert:

```sh
# ---------------------------------------------------------------------------
# 2c. Wait (bounded) for the orchestrator to deliver license/globalStorage
#     state. Only when the seed ConfigMap signalled state is expected. The
#     orchestrator streams the bundle in over SSH then touches the sentinel.
#     Bounded so a slow/absent orchestrator can't wedge startup.
# ---------------------------------------------------------------------------
if [ -f /mnt/code-server-config/expect-state ]; then
    i=0
    while [ ! -f /var/lib/code-server/.ide-seed-state-done ] && [ "$i" -lt 30 ]; do
        sleep 1; i=$((i+1))
    done
    [ -f /var/lib/code-server/.ide-seed-state-done ] || \
        echo "ide state seed sentinel timed out after ${i}s (non-fatal)" >&2
fi
```

- [x] **Step 2: Validate shell syntax**

Run: `bash -n docker/workspace-entrypoint.sh`
Expected: no output (valid)

- [x] **Step 3: Commit**

```bash
git add docker/workspace-entrypoint.sh
git commit -m "feat(ide-ext): entrypoint waits (bounded) for the state seed sentinel"
```

---

### Task B6: Provisioner writes `expect-state` + streams state post-provision

**Files:**
- Modify: `orchestrator/services/container_provisioner.py` (`_create_seed_configmap` adds the flag; add a post-provision `_seed_workspace_state` call after the pod is Ready)
- Test: manual + live (B8)

- [x] **Step 1: Add the `expect-state` flag to the ConfigMap when state exists**

In `_create_seed_configmap`, after composing `data["seed.sh"]`, when the user has a stored globalStorage signature or any `bytes` extension, add a marker file:

```python
        needs_state = bool(extensions) and (
            any(v.get("source") == "bytes" for v in extensions.values())
            or await IdeSettingsStore(self._db).get_ext_signature(str(getattr(owner, "user_id", "")) or "x")
        )
        if needs_state:
            body["data"]["expect-state"] = "1"
```

(The ConfigMap is mounted at `/mnt/code-server-config`, so `expect-state` becomes `/mnt/code-server-config/expect-state`, matching the entrypoint check.) Thread `owner` into `_create_seed_configmap` or compute `needs_state` at the call site and pass a bool — keep the signature explicit: `_create_seed_configmap(pod_name, files, extensions, needs_state=False)`.

- [x] **Step 2: Stream state after the pod is Ready**

Add a method that the post-Ready path calls (containers don't have a daemon-register hook like VMs, so reuse the existing readiness wait in `create_workspace`/`create_ide_pod` — after the pod reports Ready and `pod_ip` is known):

```python
async def _seed_workspace_state(self, owner: "WorkspaceOwner", pod_ip: str) -> None:
    """Stream globalStorage + bytes extensions into a freshly-Ready container and
    touch the sentinel. Fire-and-forget; failure leaves the IDE usable (binaries
    still came via Open VSX)."""
    user_id = getattr(owner, "user_id", None)
    if not user_id or not snapshot_service.is_available:
        return
    from services.ide_profile_store import IdeProfileStore
    from services.ide_settings import IdeSettingsStore, seed_ide_profile

    store = IdeSettingsStore(self._db)
    items = await store.get_extensions(str(user_id))
    profile = IdeProfileStore(snapshot_service._s3, snapshot_service._bucket)
    await seed_ide_profile(
        user_id=str(user_id), ssh_host=pod_ip, ssh_port=30022,
        profile_store=profile, ext_items=items,
    )
```

Call it (as a fire-and-forget task) right after the pod becomes Ready in both `create_workspace` and `create_ide_pod`:

```python
        asyncio.create_task(self._seed_workspace_state(owner, pod_ip))
```

- [x] **Step 3: Compile + lint**

Run: `python -c "import ast; ast.parse(open('orchestrator/services/container_provisioner.py').read()); print('ok')"`
Run: `ruff check orchestrator/services/container_provisioner.py`

- [x] **Step 4: Commit**

```bash
git add orchestrator/services/container_provisioner.py
git commit -m "feat(ide-ext): stream globalStorage/bytes state into Ready containers + expect-state flag"
```

---

### Task B7: Capture state on the sweep + suspend; seed state on VM/restore

**Files:**
- Modify: `orchestrator/main.py` (sweeper: call `capture_ide_profile` per workspace when S3 available)
- Modify: `orchestrator/services/workspace_suspension.py` (opportunistic `capture_ide_profile` on graceful suspend)
- Modify: `orchestrator/services/nats_bridge.py`, `ide_session.py` (after `seed_ide_config_for_user`, call `seed_ide_profile` for VM/restore)

- [x] **Step 1: Sweeper captures state (S3-gated)**

In `code_server_settings_sweeper`, after the extension reconcile, when `snapshot_service.is_available`, build one `IdeProfileStore` and call `capture_ide_profile` per workspace:

```python
            if snapshot_service.is_available:
                from services.ide_profile_store import IdeProfileStore
                from services.ide_settings import capture_ide_profile

                profile = IdeProfileStore(snapshot_service._s3, snapshot_service._bucket)
                for ws in workspaces:
                    uid = ws.get("user_id")
                    tgt = None
                    if uid:
                        from services.ide_settings import resolve_ssh_target, _coerce_context
                        tgt = resolve_ssh_target(_coerce_context(ws.get("context")))
                    if uid and tgt:
                        try:
                            await capture_ide_profile(store, str(uid), tgt[0], tgt[1], profile)
                        except Exception as e:  # noqa: BLE001
                            logger.warning("ide profile capture failed: %s", e)
```

- [x] **Step 2: Suspend + VM/restore hooks**

In `workspace_suspension.py` graceful path (where the file pull already happens), add the same `capture_ide_profile` call for `job.get("user_id")`. In `nats_bridge._seed_vm_ide_config` and `ide_session._restore_vm_session`, after the existing `seed_ide_config_for_user(...)`, add (when S3 available):

```python
        from services.ide_profile_store import IdeProfileStore
        from services.ide_settings import IdeSettingsStore, seed_ide_profile

        if snapshot_service.is_available:
            items = await IdeSettingsStore(self._db).get_extensions(str(user_id))
            profile = IdeProfileStore(snapshot_service._s3, snapshot_service._bucket)
            await seed_ide_profile(
                user_id=str(user_id), ssh_host=ssh_host, ssh_port=ssh_port,
                profile_store=profile, ext_items=items,
            )
```

- [x] **Step 3: Compile + lint all touched files**

Run: `python -c "import ast; [ast.parse(open(f).read()) for f in ['orchestrator/main.py','orchestrator/services/workspace_suspension.py','orchestrator/services/nats_bridge.py','orchestrator/services/ide_session.py']]; print('ok')"`
Run: `ruff check orchestrator/`

- [x] **Step 4: Commit**

```bash
git add orchestrator/main.py orchestrator/services/workspace_suspension.py orchestrator/services/nats_bridge.py orchestrator/services/ide_session.py
git commit -m "feat(ide-ext): capture state on sweep/suspend; seed state on VM-ready/restore"
```

---

### Task B8: Phase B live verification (Monokai Pro, no nag)

**Files:** none (verification).

- [x] **Step 1:** In a session IDE, install **Monokai Pro**, enter its license key, set the theme. Also install a deliberately **non-Open-VSX** extension (to exercise the `bytes` path).
- [x] **Step 2:** Trigger capture (graceful suspend or wait a sweep). Confirm in S3:

```bash
kubectl --context main -n superhuman-remote-worker exec srw-postgres-0 -- \
  psql -U srw -d srw -tAc "SELECT settings->'ide'->'extensions'->'sig', jsonb_pretty(settings->'ide'->'extensions'->'items') FROM users WHERE email='<user>';"
# and list the S3 prefix via the orchestrator's mc/boto, or:
kubectl --context main -n superhuman-remote-worker exec <orchestrator-pod> -- \
  python3 -c "import boto3,os;c=boto3.client('s3',endpoint_url=os.environ['S3_ENDPOINT'],aws_access_key_id=os.environ['S3_ACCESS_KEY'],aws_secret_access_key=os.environ['S3_SECRET_KEY']);print([o['Key'] for o in c.list_objects_v2(Bucket=os.environ.get('S3_BUCKET','srw-snapshots'),Prefix='ide-profiles/').get('Contents',[])])"
```
Expected: `globalStorage.tar.zst` (and an `ext/<id>/<ver>.tar.zst` for the non-Open-VSX one).

- [x] **Step 3:** Provision a fresh session. Confirm the pod gets `/mnt/code-server-config/expect-state`, the sentinel `.ide-seed-state-done` appears, globalStorage is restored, and the bytes extension folder exists.
- [x] **Step 4:** Open the new IDE → **Monokai Pro active, no license nag** on first paint; the non-Open-VSX extension present.
- [x] **Step 5:** Verification only — no commit.

---

## Self-Review

**Spec coverage:** §3 data model → A3/B1 (manifest items + S3 layout, `sig`). §4 capture → A5 (list/classify/merge), B2 (signature), B3 (globalStorage/bytes upload), B7 (sweep/suspend wiring). §5 seed channel (a) → A4/A7/A8 (install script + ConfigMap + VM/restore); channel (b) + sentinel → B4/B5/B6/B7. §6 edge cases → install `|| true` (A4), classify defaults to bytes (A2), signature-skip (B3), bounded sentinel (B5), `is_available`/`get` returns False guards (B1/B6). §7 components → all files have tasks. §8 testing → unit tasks + A9/B8 live.

**Placeholder scan:** One deliberate implementer note in B4 (the `_push_fn`/`_ok` helper) — it specifies the exact transport (reverse of `_ssh_tar_to_file`) and TDD approach, not a vague TODO. The stray placeholder in A7 Step 2's first code block is corrected by the second, canonical block in the same step (use the second).

**Type consistency:** manifest item shape `{version, source, theme}` is consistent across A1 (`{version, theme}` pre-classify) → A5 (adds `source`) → A3 store → A4 install. `OpenVsxClassifier.classify(id, version)->str`, `IdeProfileStore` keys, `seed_ide_profile(*, user_id, ssh_host, ssh_port, profile_store, ext_items, ...)`, and `capture_ide_profile(store, user_id, host, port, profile_store, ...)` are used identically in main.py/provisioner/hooks. `resolve_ssh_target`/`_coerce_context` reused as-is.

**Dependency note:** Phase B reaches into `snapshot_service._s3`/`._bucket`. If a cleaner accessor is wanted, add `SnapshotService.s3_client`/`bucket` properties in B1 and use those — functionally identical, avoids the private-attribute reach.
