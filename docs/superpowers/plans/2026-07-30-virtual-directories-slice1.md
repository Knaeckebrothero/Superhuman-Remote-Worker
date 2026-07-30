# Virtual Directories — Slice 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Serve `tools/`, `contacts/`, `instructions.md`, and `task_brief.md` to the agent from live state through a workspace-backend overlay, so no framework files are ever written to the workspace filesystem.

**Architecture:** A `VirtualOverlayBackend` wraps the real workspace backend inside `WorkspaceManager`. It routes path operations whose first segment matches a registered provider prefix to that provider (`entries()` / `read()`), and delegates everything else to the wrapped backend via `__getattr__` — the same duck-typed proxy idiom as the existing `SubdirBackend`. Providers are registered at the three boot paths that write these files today. Slice 1 providers are read-only; the contract carries `writable` + `write()` so Slice 2 (Postgres-backed `plan.md` / `todos.yaml`) adds a provider instead of forcing a redesign.

**Tech Stack:** Python 3.12 (CI gate), pytest, httpx (sync client for the contacts fetch), FastAPI (orchestrator endpoint), existing `DescriptionManager` renderers.

**Spec:** `docs/features/virtual_directories.md`

## Global Constraints

- Work on `develop`. No sub-branches. Commit per task. **Never push without asking.**
- CI runs Python 3.12 and is the merge gate; local pytest is noisy on 3.14 — trust CI.
- `ruff` runs on push; keep imports ordered and lines within the project's existing style.
- Kill switch: `VIRTUAL_DIRS_ENABLED` (env, default `true`). When false the overlay is not installed and **no materialization fallback exists** — the legacy write path is deleted, not disabled.
- Virtual trees are **flat**: no subdirectories inside a virtual prefix (per arXiv 2607.17598 — a second routing level degrades accuracy).
- Slice 1 providers are **read-only**. `plan.md` / `todos.yaml` / `job_documents` are **out of scope for this plan** (Slice 2).
- Skills stay materialized as real files — skill scripts are shell-executed.
- No new agent tools (capability-surface cost rule): discovery comes from the file projection.
- All paths are workspace-relative and POSIX (`posixpath`), never `os.path`.
- Virtual content is served to the shell **never** — `run_command` runs over SSH against the real filesystem.

## File Structure

| File | Responsibility |
|---|---|
| `src/core/backends/overlay.py` (create) | `VirtualPathError`, `EntryMeta`, `VirtualDirProvider` protocol, `VirtualOverlayBackend` |
| `src/core/virtual_dirs/__init__.py` (create) | Provider package exports |
| `src/core/virtual_dirs/tools_provider.py` (create) | `ToolsProvider` — renders from the live tool list |
| `src/core/virtual_dirs/single_file.py` (create) | `SingleFileProvider` — one virtual file from a render callable |
| `src/core/virtual_dirs/contacts_provider.py` (create) | `ContactsProvider` — TTL-cached fetch from the orchestrator |
| `src/core/workspace.py` (modify) | Wrap backend in the overlay; `register_virtual_provider()` |
| `src/agent.py` (modify) | Register providers; delete tool-doc + instructions writes; retarget sentinel probes |
| `src/api/persistent_session.py` (modify) | Register providers; delete tool-doc + instructions writes |
| `src/tools/delegation/reader_env.py` (modify) | Register a reader-scoped `ToolsProvider` |
| `orchestrator/routers/contacts.py` (modify) | Internal job/thread-keyed contacts endpoint |
| `tests/test_virtual_overlay.py` (create) | Overlay unit matrix |
| `tests/test_virtual_providers.py` (create) | Provider unit tests |
| `tests/test_virtual_dirs_wiring.py` (create) | Wiring, sweep, and sentinel-probe regression |

---

### Task 1: Overlay core — contract, routing, and the read path

**Files:**
- Create: `src/core/backends/overlay.py`
- Test: `tests/test_virtual_overlay.py`

**Interfaces:**
- Consumes: `WorkspaceBackend` (`src/core/workspace_backend.py:203`), `FilesystemTestBackend` (`tests/_fs_backend.py:16`)
- Produces: `VirtualPathError(ValueError)`; `EntryMeta(size: int, mtime: float | None)`; `VirtualDirProvider` protocol with `prefix: str`, `is_dir: bool`, `writable: bool`, `entries() -> dict[str, EntryMeta]`, `read(name: str) -> str | None`, `write(name: str, content: str) -> None`; `VirtualOverlayBackend(inner)` with `.inner`, `.register(provider)`, `.providers`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_virtual_overlay.py
import pytest

from src.core.backends.overlay import EntryMeta, VirtualOverlayBackend
from tests._fs_backend import FilesystemTestBackend


class FakeProvider:
    """Read-only directory provider serving two fixed files."""

    prefix = "tools"
    is_dir = True
    writable = False

    def __init__(self, docs=None):
        self.docs = docs if docs is not None else {
            "README.md": "# Available Tools\n\n- read_file\n",
            "read_file.md": "# read_file\n\nReads a file.\n",
        }
        self.read_calls = 0

    def entries(self):
        return {
            name: EntryMeta(size=len(body.encode("utf-8")))
            for name, body in self.docs.items()
        }

    def read(self, name):
        self.read_calls += 1
        return self.docs.get(name)


@pytest.fixture
def overlay(tmp_path):
    inner = FilesystemTestBackend(tmp_path)
    ov = VirtualOverlayBackend(inner)
    ov.register(FakeProvider())
    return ov


def test_reads_virtual_file(overlay):
    assert "Reads a file." in overlay.read_file("tools/read_file.md")


def test_unknown_name_under_prefix_is_not_found_not_fallthrough(overlay, tmp_path):
    # A stale real file must NEVER be served once the prefix is virtual.
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "removed_tool.md").write_text("STALE")
    with pytest.raises(FileNotFoundError):
        overlay.read_file("tools/removed_tool.md")


def test_real_paths_delegate(overlay, tmp_path):
    (tmp_path / "notes.md").write_text("real")
    assert overlay.read_file("notes.md") == "real"


def test_nested_prefix_is_not_virtual(overlay, tmp_path):
    # `main/tools/` inside a cloned repo must stay real.
    (tmp_path / "main" / "tools").mkdir(parents=True)
    (tmp_path / "main" / "tools" / "x.md").write_text("repo file")
    assert overlay.read_file("main/tools/x.md") == "repo file"


def test_path_forms_normalize(overlay):
    for form in ("tools/README.md", "./tools/README.md", "/tools/README.md"):
        assert "Available Tools" in overlay.read_file(form)


def test_binary_read_returns_bytes(overlay):
    assert overlay.read_file("tools/README.md", binary=True).startswith(b"# Available")


def test_existence_and_type_predicates(overlay):
    assert overlay.exists("tools") and overlay.is_dir("tools")
    assert overlay.exists("tools/README.md") and overlay.is_file("tools/README.md")
    assert not overlay.is_dir("tools/README.md")
    assert not overlay.exists("tools/nope.md")


def test_stat_reports_rendered_size(overlay):
    assert overlay.stat("tools/README.md") == len(
        "# Available Tools\n\n- read_file\n".encode("utf-8")
    )


def test_stat_returns_zero_for_a_missing_virtual_entry(overlay):
    # WorkspaceBackend.stat contract (workspace_backend.py:448): "0 if path
    # doesn't exist". RemoteBackend and FilesystemTestBackend both honour it;
    # a virtual path must not diverge.
    assert overlay.stat("tools/nope.md") == 0


def test_provider_failure_surfaces_readable_error(tmp_path):
    class Broken(FakeProvider):
        def read(self, name):
            raise RuntimeError("upstream down")

    ov = VirtualOverlayBackend(FilesystemTestBackend(tmp_path))
    ov.register(Broken())
    with pytest.raises(ValueError, match="temporarily unavailable"):
        ov.read_file("tools/README.md")


def test_unknown_attributes_delegate_to_inner(overlay):
    assert overlay.root == overlay.inner.root
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_virtual_overlay.py -x -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.core.backends.overlay'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/core/backends/overlay.py
"""VirtualOverlayBackend — agent-visible directories served from live state.

Wraps a real ``WorkspaceBackend`` and answers file operations for registered
virtual prefixes (``tools/``, ``contacts/``, ``instructions.md``) from
providers instead of the workspace filesystem. Everything else delegates.

Duck-typed proxy (not a ``WorkspaceBackend`` subclass), mirroring
``SubdirBackend``: anything not overridden passes through via ``__getattr__``.
No isinstance checks target backend types in the codebase, so this is safe.

See docs/features/virtual_directories.md.
"""

import logging
import posixpath
from dataclasses import dataclass
from typing import Any, Dict, Optional, Protocol, Tuple

logger = logging.getLogger(__name__)


class VirtualPathError(ValueError):
    """An operation is not permitted on a virtual path.

    Subclasses ``ValueError`` so the file tools' existing path-error handling
    surfaces it to the agent as a readable message.
    """


@dataclass(frozen=True)
class EntryMeta:
    """Metadata for one virtual entry. ``mtime`` is unused in Slice 1."""

    size: int
    mtime: Optional[float] = None


class VirtualDirProvider(Protocol):
    """Serves one virtual prefix. Flat — no subdirectories."""

    prefix: str
    is_dir: bool
    writable: bool

    def entries(self) -> Dict[str, EntryMeta]: ...

    def read(self, name: str) -> Optional[str]: ...

    def write(self, name: str, content: str) -> None: ...


class VirtualOverlayBackend:
    """Routes registered prefixes to providers; delegates everything else."""

    def __init__(self, inner: Any):
        self._inner = inner
        self._providers: Dict[str, Any] = {}

    # --- registry ---------------------------------------------------------

    @property
    def inner(self) -> Any:
        """The unwrapped backend. Use for probes that must bypass virtual paths."""
        return self._inner

    @property
    def providers(self) -> Dict[str, Any]:
        return dict(self._providers)

    def register(self, provider: Any) -> None:
        self._providers[provider.prefix] = provider
        logger.debug("Registered virtual provider: %s", provider.prefix)

    # --- routing ----------------------------------------------------------

    @staticmethod
    def _normalize(path: str) -> str:
        p = (path or "").strip()
        while p.startswith("./"):
            p = p[2:]
        p = p.strip("/")
        if not p:
            return ""
        return posixpath.normpath(p)

    def _match(self, path: str) -> Optional[Tuple[Any, str]]:
        """Return ``(provider, entry_name)`` when *path* is virtual.

        ``entry_name`` is ``""`` for the directory itself. For a single-file
        provider the entry name is the prefix.
        """
        p = self._normalize(path)
        if not p:
            return None
        head = p.split("/", 1)[0]
        provider = self._providers.get(head)
        if provider is None:
            return None
        if not provider.is_dir:
            return (provider, p) if p == provider.prefix else (provider, "")
        return provider, p[len(head):].lstrip("/")

    # --- provider calls, never allowed to crash the agent loop ------------

    def _entries(self, provider: Any) -> Dict[str, EntryMeta]:
        try:
            return provider.entries()
        except Exception as e:  # degrade to empty listing, never explode
            logger.warning("virtual entries() failed for %s: %s", provider.prefix, e)
            return {}

    def _read(self, provider: Any, name: str, path: str) -> Optional[str]:
        try:
            return provider.read(name)
        except Exception as e:
            logger.warning("virtual read failed for %s: %s", path, e)
            raise VirtualPathError(f"{path} is temporarily unavailable: {e}") from e

    # --- read path --------------------------------------------------------

    def read_file(self, path: str, binary: bool = False) -> Any:
        m = self._match(path)
        if m is None:
            return self._inner.read_file(path, binary=binary)
        provider, name = m
        content = self._read(provider, name, path) if name else None
        if content is None:
            raise FileNotFoundError(f"File not found: {path}")
        return content.encode("utf-8") if binary else content

    def exists(self, path: str) -> bool:
        m = self._match(path)
        if m is None:
            return self._inner.exists(path)
        provider, name = m
        if not name:
            return bool(provider.is_dir)
        return name in self._entries(provider)

    def is_file(self, path: str) -> bool:
        m = self._match(path)
        if m is None:
            return self._inner.is_file(path)
        provider, name = m
        return bool(name) and name in self._entries(provider)

    def is_dir(self, path: str) -> bool:
        m = self._match(path)
        if m is None:
            return self._inner.is_dir(path)
        provider, name = m
        return bool(provider.is_dir) and not name

    def stat(self, path: str) -> int:
        m = self._match(path)
        if m is None:
            return self._inner.stat(path)
        provider, name = m
        entries = self._entries(provider)
        if not name:
            return sum(meta.size for meta in entries.values())
        meta = entries.get(name)
        # Contract (workspace_backend.py:448): 0 for a missing path, never an
        # exception. Both real backends honour it; virtual paths must too.
        return meta.size if meta is not None else 0

    # --- delegation -------------------------------------------------------

    def __getattr__(self, name: str) -> Any:
        # Private names are never delegated: this also stops __getattr__ from
        # recursing on self._inner before __init__ has run.
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._inner, name)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_virtual_overlay.py -x -q`
Expected: PASS (10 tests)

- [ ] **Step 5: Commit**

```bash
git add src/core/backends/overlay.py tests/test_virtual_overlay.py
git commit -m "feat(workspace): virtual overlay backend — provider contract and read path"
```

---

### Task 2: Listings and search

**Files:**
- Modify: `src/core/backends/overlay.py`
- Test: `tests/test_virtual_overlay.py`

**Interfaces:**
- Consumes: Task 1's `VirtualOverlayBackend`, `_match`, `_entries`
- Produces: `list_dir(path="", pattern="*") -> list[str]` (virtual dirs appear as `"tools/"`, virtual files as `"instructions.md"`); `search_files(query, path="", case_sensitive=False, exclude_dirs=None) -> list[dict]` with keys `path`, `line_number`, `line`

- [ ] **Step 1: Write the failing test**

Add this import to the **existing import block at the top of the file** — not mid-file, which trips ruff `E402` and fails the blocking `main` lint gate:

```python
from src.core.workspace_backend import SEARCH_RESULT_HARD_CAP
```

Then append the test functions:

```python
# append to tests/test_virtual_overlay.py
def test_root_listing_merges_virtual_prefix(overlay, tmp_path):
    (tmp_path / "notes.md").write_text("hi")
    listing = overlay.list_dir("")
    assert "notes.md" in listing
    assert "tools/" in listing


def test_root_listing_dedupes_real_leftover(overlay, tmp_path):
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "stale.md").write_text("x")
    assert overlay.list_dir("").count("tools/") == 1


def test_listing_inside_prefix_comes_from_provider(overlay, tmp_path):
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "stale.md").write_text("x")
    assert sorted(overlay.list_dir("tools")) == ["tools/README.md", "tools/read_file.md"]


def test_listing_respects_pattern(overlay):
    assert overlay.list_dir("tools", "READ*") == ["tools/README.md"]


def test_nonroot_listing_gets_no_virtual_entries(overlay, tmp_path):
    (tmp_path / "sub").mkdir()
    assert overlay.list_dir("sub") == []


def test_search_inside_prefix_searches_provider_only(overlay, tmp_path):
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "stale.md").write_text("Reads a file.")
    hits = overlay.search_files("Reads a file", path="tools")
    assert [h["path"] for h in hits] == ["tools/read_file.md"]
    assert hits[0]["line_number"] == 3


def test_search_scoped_to_a_single_virtual_file(overlay):
    # Naming one entry must search THAT entry, never its siblings.
    hits = overlay.search_files("Reads a file", path="tools/read_file.md")
    assert [h["path"] for h in hits] == ["tools/read_file.md"]
    assert overlay.search_files("Available Tools", path="tools/read_file.md") == []


def test_listing_below_a_virtual_entry_is_empty(overlay):
    assert overlay.list_dir("tools/README.md") == []


def test_root_search_merges_real_and_virtual(overlay, tmp_path):
    (tmp_path / "notes.md").write_text("read_file is handy\n")
    paths = {h["path"] for h in overlay.search_files("read_file")}
    assert "notes.md" in paths
    assert "tools/README.md" in paths


def test_search_is_case_insensitive_by_default(overlay):
    assert overlay.search_files("READS A FILE")
    assert not overlay.search_files("READS A FILE", case_sensitive=True)


def test_search_respects_hard_cap(tmp_path):
    body = "\n".join(["needle"] * (SEARCH_RESULT_HARD_CAP + 50))

    class Big(FakeProvider):
        def __init__(self):
            super().__init__({"big.md": body})

    ov = VirtualOverlayBackend(FilesystemTestBackend(tmp_path))
    ov.register(Big())
    assert len(ov.search_files("needle")) == SEARCH_RESULT_HARD_CAP
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_virtual_overlay.py -x -q -k "listing or search"`
Expected: FAIL — `list_dir` delegates to the inner backend, so `"tools/"` is absent from the root listing

- [ ] **Step 3: Write minimal implementation**

Add to `src/core/backends/overlay.py` — extend the import line to `import fnmatch` at the top, and add these methods to `VirtualOverlayBackend` after `stat`:

```python
    def list_dir(self, path: str = "", pattern: str = "*") -> list:
        m = self._match(path)
        if m is not None:
            provider, name = m
            if name or not provider.is_dir:
                return []  # flat: nothing lives below a virtual entry
            return [
                f"{provider.prefix}/{n}"
                for n in sorted(self._entries(provider))
                if fnmatch.fnmatch(n, pattern)
            ]

        results = list(self._inner.list_dir(path, pattern))
        if self._normalize(path):
            return results  # only the workspace root gains virtual entries

        seen = {r.rstrip("/") for r in results}
        for provider in self._providers.values():
            if provider.prefix in seen:
                continue
            if fnmatch.fnmatch(provider.prefix, pattern):
                results.append(
                    f"{provider.prefix}/" if provider.is_dir else provider.prefix
                )
        return results

    def _search_provider(
        self, provider: Any, query: str, case_sensitive: bool
    ) -> list:
        needle = query if case_sensitive else query.lower()
        hits = []
        for name in sorted(self._entries(provider)):
            try:
                content = provider.read(name) or ""
            except Exception as e:
                logger.warning("virtual search skipped %s/%s: %s", provider.prefix, name, e)
                continue
            rel = f"{provider.prefix}/{name}" if provider.is_dir else name
            for lineno, line in enumerate(content.splitlines(), 1):
                haystack = line if case_sensitive else line.lower()
                if needle in haystack:
                    hits.append({"path": rel, "line_number": lineno, "line": line})
        return hits

    def search_files(
        self,
        query: str,
        path: str = "",
        case_sensitive: bool = False,
        exclude_dirs: Optional[list] = None,
    ) -> list:
        m = self._match(path)
        if m is not None:
            provider, name = m
            hits = self._search_provider(provider, query, case_sensitive)
            if name:
                # A path naming one entry scopes the search to that entry —
                # discarding `name` here would report hits under sibling files
                # the caller never named.
                target = f"{provider.prefix}/{name}" if provider.is_dir else name
                hits = [hit for hit in hits if hit["path"] == target]
            return hits[:SEARCH_RESULT_HARD_CAP]

        results = list(
            self._inner.search_files(query, path, case_sensitive, exclude_dirs)
        )
        if not self._normalize(path):
            for provider in self._providers.values():
                if exclude_dirs and provider.prefix in exclude_dirs:
                    continue
                results.extend(
                    self._search_provider(provider, query, case_sensitive)
                )
        return results[:SEARCH_RESULT_HARD_CAP]
```

Add the cap import near the top of the file (after `logger = ...` is fine, but module level is cleaner):

```python
from ..workspace_backend import SEARCH_RESULT_HARD_CAP
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_virtual_overlay.py -q`
Expected: PASS (all Task 1 + Task 2 tests)

- [ ] **Step 5: Commit**

```bash
git add src/core/backends/overlay.py tests/test_virtual_overlay.py
git commit -m "feat(workspace): virtual overlay listings and search merge"
```

---

### Task 3: Mutation rejection, copy-out escape hatch, writable routing

**Files:**
- Modify: `src/core/backends/overlay.py`
- Test: `tests/test_virtual_overlay.py`

**Interfaces:**
- Consumes: Task 1's `VirtualPathError`, `_match`
- Produces: `write_file`, `append_file`, `mkdir`, `delete_file`, `delete_directory`, `move`, `copy` overrides. Read-only virtual paths raise `VirtualPathError`; `copy` from virtual to real succeeds; `writable=True` providers receive `provider.write(name, content)`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_virtual_overlay.py
from src.core.backends.overlay import VirtualPathError


@pytest.mark.parametrize(
    "op",
    [
        lambda ov: ov.write_file("tools/x.md", "nope"),
        lambda ov: ov.append_file("tools/README.md", "nope"),
        lambda ov: ov.mkdir("tools/sub"),
        lambda ov: ov.delete_file("tools/README.md"),
        lambda ov: ov.delete_directory("tools"),
        lambda ov: ov.move("tools/README.md", "copy.md"),
        lambda ov: ov.move("real.md", "tools/README.md"),
        lambda ov: ov.copy("real.md", "tools/x.md"),
    ],
)
def test_mutations_on_readonly_virtual_paths_are_rejected(overlay, tmp_path, op):
    (tmp_path / "real.md").write_text("real")
    with pytest.raises(VirtualPathError) as excinfo:
        op(overlay)
    assert "virtual" in str(excinfo.value).lower()


def test_copy_out_of_virtual_to_real_is_allowed(overlay, tmp_path):
    overlay.copy("tools/README.md", "my_tools.md")
    assert "Available Tools" in (tmp_path / "my_tools.md").read_text()


def test_real_to_real_mutations_still_delegate(overlay, tmp_path):
    overlay.write_file("scratch.md", "content")
    assert (tmp_path / "scratch.md").read_text() == "content"
    overlay.delete_file("scratch.md")
    assert not (tmp_path / "scratch.md").exists()


def test_writable_provider_receives_writes(tmp_path):
    class Writable(FakeProvider):
        prefix = "plan.md"
        is_dir = False
        writable = True

        def __init__(self):
            super().__init__({"plan.md": "# Plan\n"})
            self.written = None

        def write(self, name, content):
            self.written = (name, content)
            self.docs[name] = content

    provider = Writable()
    ov = VirtualOverlayBackend(FilesystemTestBackend(tmp_path))
    ov.register(provider)
    ov.write_file("plan.md", "# New Plan\n")
    assert provider.written == ("plan.md", "# New Plan\n")
    assert ov.read_file("plan.md") == "# New Plan\n"


def test_writable_provider_receives_appends(tmp_path):
    class Writable(FakeProvider):
        prefix = "plan.md"
        is_dir = False
        writable = True

        def write(self, name, content):
            self.docs[name] = content

    ov = VirtualOverlayBackend(FilesystemTestBackend(tmp_path))
    ov.register(Writable({"plan.md": "# Plan\n"}))
    ov.append_file("plan.md", "- step one\n")
    assert ov.read_file("plan.md") == "# Plan\n- step one\n"


def test_writable_provider_write_failure_is_a_readable_error(tmp_path):
    class Exploding(FakeProvider):
        prefix = "plan.md"
        is_dir = False
        writable = True

        def write(self, name, content):
            raise RuntimeError("database down")

    ov = VirtualOverlayBackend(FilesystemTestBackend(tmp_path))
    ov.register(Exploding({"plan.md": "# Plan\n"}))
    with pytest.raises(ValueError, match="could not be written"):
        ov.write_file("plan.md", "# New\n")


def test_writable_provider_still_rejects_delete(tmp_path):
    class Writable(FakeProvider):
        prefix = "plan.md"
        is_dir = False
        writable = True

        def write(self, name, content):
            self.docs[name] = content

    ov = VirtualOverlayBackend(FilesystemTestBackend(tmp_path))
    ov.register(Writable({"plan.md": "x"}))
    with pytest.raises(VirtualPathError):
        ov.delete_file("plan.md")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_virtual_overlay.py -x -q -k "mutation or copy or writable"`
Expected: FAIL — `write_file` currently delegates to the inner backend and writes a real file instead of raising

- [ ] **Step 3: Write minimal implementation**

Add to `VirtualOverlayBackend`:

```python
    _DENY_TEMPLATE = (
        "{path} is inside the virtual directory '{prefix}', which is generated "
        "from live state and cannot be {verb}. Copy a file out (copy "
        "'{path}' to a normal workspace path) if you want an editable version."
    )

    def _deny(self, path: str, provider: Any, verb: str) -> None:
        raise VirtualPathError(
            self._DENY_TEMPLATE.format(path=path, prefix=provider.prefix, verb=verb)
        )

    def _write(self, provider: Any, name: str, content: str, path: str) -> None:
        """Mirror of ``_read``: a provider write must never crash the loop."""
        try:
            provider.write(name, content)
        except Exception as e:
            logger.warning("virtual write failed for %s: %s", path, e)
            raise VirtualPathError(f"{path} could not be written: {e}") from e

    def write_file(self, path: str, content: Any) -> None:
        m = self._match(path)
        if m is None:
            return self._inner.write_file(path, content)
        provider, name = m
        if not provider.writable or not name:
            self._deny(path, provider, "written to")
        self._write(provider, name, content, path)

    def append_file(self, path: str, content: str) -> None:
        m = self._match(path)
        if m is None:
            return self._inner.append_file(path, content)
        provider, name = m
        if not provider.writable or not name:
            self._deny(path, provider, "appended to")
        existing = self._read(provider, name, path) or ""
        self._write(provider, name, existing + content, path)

    def mkdir(self, path: str) -> None:
        m = self._match(path)
        if m is None:
            return self._inner.mkdir(path)
        self._deny(path, m[0], "created")

    def delete_file(self, path: str) -> bool:
        m = self._match(path)
        if m is None:
            return self._inner.delete_file(path)
        self._deny(path, m[0], "deleted")

    def delete_directory(self, path: str) -> bool:
        m = self._match(path)
        if m is None:
            return self._inner.delete_directory(path)
        self._deny(path, m[0], "deleted")

    def move(self, src: str, dst: str) -> None:
        for candidate in (src, dst):
            m = self._match(candidate)
            if m is not None:
                self._deny(candidate, m[0], "moved")
        return self._inner.move(src, dst)

    def copy(self, src: str, dst: str) -> None:
        dst_match = self._match(dst)
        if dst_match is not None:
            self._deny(dst, dst_match[0], "copied onto")
        src_match = self._match(src)
        if src_match is None:
            return self._inner.copy(src, dst)
        # Copy-out: the escape hatch the denial message advertises.
        provider, name = src_match
        content = self._read(provider, name, src) if name else None
        if content is None:
            raise FileNotFoundError(f"File not found: {src}")
        self._inner.write_file(dst, content)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_virtual_overlay.py -q`
Expected: PASS (all overlay tests)

- [ ] **Step 5: Commit**

```bash
git add src/core/backends/overlay.py tests/test_virtual_overlay.py
git commit -m "feat(workspace): virtual path mutation rules and copy-out escape hatch"
```

---

### Task 4: ToolsProvider and SingleFileProvider

**Files:**
- Create: `src/core/virtual_dirs/__init__.py`, `src/core/virtual_dirs/tools_provider.py`, `src/core/virtual_dirs/single_file.py`
- Test: `tests/test_virtual_providers.py`

**Interfaces:**
- Consumes: `EntryMeta` (Task 1); `DescriptionManager` (`src/tools/description_manager.py:18`) with `extract_docstrings(tools)`, `generate_tool_index(names)`, `generate_tool_description(name)`
- Produces: `ToolsProvider(get_tools: Callable[[], list])` with `prefix="tools"`, `is_dir=True`; `SingleFileProvider(prefix: str, render: Callable[[], str])` with `is_dir=False`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_virtual_providers.py
from src.core.virtual_dirs import SingleFileProvider, ToolsProvider
from src.tools.description_manager import generate_tool_index


class FakeTool:
    def __init__(self, name, description):
        self.name = name
        self.description = description


def test_tools_provider_lists_index_and_each_tool():
    provider = ToolsProvider(lambda: [FakeTool("read_file", "Reads a file.")])
    assert set(provider.entries()) == {"README.md", "read_file.md"}


def test_readme_matches_canonical_renderer():
    tools = [FakeTool("read_file", "Reads a file.")]
    provider = ToolsProvider(lambda: tools)
    assert provider.read("README.md") == generate_tool_index(["read_file"])


def test_tool_doc_contains_full_docstring():
    provider = ToolsProvider(lambda: [FakeTool("read_file", "Reads a file fully.")])
    assert "Reads a file fully." in provider.read("read_file.md")


def test_unknown_tool_returns_none():
    provider = ToolsProvider(lambda: [FakeTool("read_file", "d")])
    assert provider.read("gone.md") is None


def test_tool_list_changes_are_reflected_without_reregistration():
    """The workspace-upgrade re-derive changes the tool list mid-lifecycle."""
    tools = [FakeTool("read_file", "d")]
    provider = ToolsProvider(lambda: tools)
    assert "run_command.md" not in provider.entries()
    tools.append(FakeTool("run_command", "Runs a command."))
    assert "run_command.md" in provider.entries()
    assert "Runs a command." in provider.read("run_command.md")


def test_provider_flags():
    provider = ToolsProvider(lambda: [])
    assert provider.prefix == "tools" and provider.is_dir and not provider.writable


def test_single_file_provider_serves_one_entry():
    provider = SingleFileProvider("instructions.md", lambda: "# Instructions\n")
    assert set(provider.entries()) == {"instructions.md"}
    assert provider.read("instructions.md") == "# Instructions\n"
    assert provider.read("other.md") is None
    assert not provider.is_dir and not provider.writable


def test_single_file_provider_renders_lazily():
    calls = []

    def render():
        calls.append(1)
        return "body"

    provider = SingleFileProvider("task_brief.md", render)
    provider.read("task_brief.md")
    provider.read("task_brief.md")
    assert len(calls) == 2  # always live, never cached
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_virtual_providers.py -x -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.core.virtual_dirs'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/core/virtual_dirs/single_file.py
"""SingleFileProvider — one virtual file rendered from a callable."""

from typing import Callable, Dict, Optional

from ..backends.overlay import EntryMeta


class SingleFileProvider:
    """Serves exactly one virtual file at ``prefix``."""

    is_dir = False
    writable = False

    def __init__(self, prefix: str, render: Callable[[], str]):
        self.prefix = prefix
        self._render = render

    def entries(self) -> Dict[str, EntryMeta]:
        body = self._render() or ""
        return {self.prefix: EntryMeta(size=len(body.encode("utf-8")))}

    def read(self, name: str) -> Optional[str]:
        if name != self.prefix:
            return None
        return self._render()
```

```python
# src/core/virtual_dirs/tools_provider.py
"""ToolsProvider — tools/ rendered from the currently loaded tool objects.

Renders through the canonical DescriptionManager helpers, so the virtual
content is byte-identical to what the deleted materialization wrote. The tool
list is read on every call, so mid-lifecycle changes (virtual->sandbox upgrade
re-derive, session tool-group overrides) are reflected with no regeneration.
"""

from typing import Callable, Dict, List, Optional

from ...tools.description_manager import DescriptionManager
from ..backends.overlay import EntryMeta


class ToolsProvider:
    prefix = "tools"
    is_dir = True
    writable = False

    def __init__(self, get_tools: Callable[[], List]):
        self._get_tools = get_tools
        self._manager = DescriptionManager()

    def _render_all(self) -> Dict[str, str]:
        tools = list(self._get_tools() or [])
        self._manager.extract_docstrings(tools)
        names = [t.name for t in tools]
        docs = {"README.md": self._manager.generate_tool_index(names)}
        for name in names:
            docs[f"{name}.md"] = self._manager.generate_tool_description(name)
        return docs

    def entries(self) -> Dict[str, EntryMeta]:
        return {
            name: EntryMeta(size=len(body.encode("utf-8")))
            for name, body in self._render_all().items()
        }

    def read(self, name: str) -> Optional[str]:
        return self._render_all().get(name)
```

```python
# src/core/virtual_dirs/__init__.py
"""Virtual directory providers. See docs/features/virtual_directories.md."""

from .single_file import SingleFileProvider
from .tools_provider import ToolsProvider

__all__ = ["SingleFileProvider", "ToolsProvider"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_virtual_providers.py -q`
Expected: PASS (8 tests)

- [ ] **Step 5: Commit**

```bash
git add src/core/virtual_dirs tests/test_virtual_providers.py
git commit -m "feat(workspace): tools and single-file virtual providers"
```

---

### Task 5: Wire the overlay in and delete the tool-doc write path

**Files:**
- Modify: `src/core/workspace.py:301` (backend assignment)
- Modify: `src/agent.py` (`_setup_job_tools`, ~2854-2863)
- Modify: `src/api/persistent_session.py:1606-1621`
- Modify: `src/tools/delegation/reader_env.py` (after `reader_ws` construction, ~156)
- Modify: `src/tools/description_manager.py` (delete write-path wrappers)
- Modify: `src/tools/__init__.py` (drop the removed exports)
- Test: `tests/test_virtual_dirs_wiring.py`

**Interfaces:**
- Consumes: `VirtualOverlayBackend` (Task 1), `ToolsProvider` (Task 4)
- Produces: `WorkspaceManager.register_virtual_provider(provider) -> None`; `WorkspaceManager.virtual_overlay -> VirtualOverlayBackend | None`; `sweep_legacy_tools_dir(backend) -> bool` in `src/core/virtual_dirs/__init__.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_virtual_dirs_wiring.py
import pytest

from src.core.backends.overlay import VirtualOverlayBackend
from src.core.virtual_dirs import ToolsProvider, sweep_legacy_tools_dir
from src.core.workspace import WorkspaceManager, WorkspaceManagerConfig
from tests._fs_backend import FilesystemTestBackend


class FakeTool:
    def __init__(self, name, description):
        self.name = name
        self.description = description


def _manager(tmp_path, monkeypatch, enabled="true"):
    monkeypatch.setenv("VIRTUAL_DIRS_ENABLED", enabled)
    return WorkspaceManager(
        job_id="job-1",
        config=WorkspaceManagerConfig(base_path=str(tmp_path)),
        backend=FilesystemTestBackend(tmp_path),
    )


def test_manager_wraps_backend_in_overlay(tmp_path, monkeypatch):
    ws = _manager(tmp_path, monkeypatch)
    assert isinstance(ws.backend, VirtualOverlayBackend)


def test_kill_switch_leaves_backend_unwrapped(tmp_path, monkeypatch):
    ws = _manager(tmp_path, monkeypatch, enabled="false")
    assert not isinstance(ws.backend, VirtualOverlayBackend)
    assert ws.virtual_overlay is None


def test_registered_provider_serves_reads_through_the_manager(tmp_path, monkeypatch):
    ws = _manager(tmp_path, monkeypatch)
    ws.register_virtual_provider(ToolsProvider(lambda: [FakeTool("read_file", "Reads.")]))
    assert "Reads." in ws.read_file("tools/read_file.md")
    assert "tools/" in ws.backend.list_dir("")


def test_register_is_a_noop_when_disabled(tmp_path, monkeypatch):
    ws = _manager(tmp_path, monkeypatch, enabled="false")
    ws.register_virtual_provider(ToolsProvider(lambda: []))  # must not raise


def test_sweep_removes_generated_tools_dir(tmp_path):
    backend = FilesystemTestBackend(tmp_path)
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "README.md").write_text("# Available Tools\n\nold\n")
    (tmp_path / "tools" / "old_tool.md").write_text("stale")
    assert sweep_legacy_tools_dir(backend) is True
    assert not (tmp_path / "tools").exists()


def test_sweep_preserves_a_user_owned_tools_dir(tmp_path):
    backend = FilesystemTestBackend(tmp_path)
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "README.md").write_text("# My own build tools\n")
    assert sweep_legacy_tools_dir(backend) is False
    assert (tmp_path / "tools" / "README.md").exists()


def test_sweep_is_non_fatal_without_a_tools_dir(tmp_path):
    assert sweep_legacy_tools_dir(FilesystemTestBackend(tmp_path)) is False


def test_legacy_write_helpers_are_gone():
    import src.tools as tools_pkg

    assert not hasattr(tools_pkg, "generate_workspace_tool_docs")


def test_provider_serves_full_docstrings_after_overrides_rebind(tmp_path, monkeypatch):
    """Deferred tools must reach tools/<name>.md with FULL docstrings.

    apply_description_overrides() returns copies carrying short blurbs and the
    caller rebinds its tool attribute to them. A provider bound to that
    rebound attribute would serve blurbs — defeating the deferred-tool design.
    The provider must hold the pre-override objects.
    """
    from src.tools.description_manager import apply_description_overrides

    full_tools = [FakeTool("read_file", "Full docstring, every argument explained.")]
    ws = _manager(tmp_path, monkeypatch)
    ws.register_virtual_provider(ToolsProvider(lambda: full_tools))

    # Simulate the boot sequence: overrides run and rebind the agent's list.
    _rebound = apply_description_overrides(list(full_tools))

    assert "Full docstring, every argument explained." in ws.read_file(
        "tools/read_file.md"
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_virtual_dirs_wiring.py -x -q`
Expected: FAIL — `ImportError: cannot import name 'sweep_legacy_tools_dir'`

- [ ] **Step 3: Write minimal implementation**

**3a.** Add the sweep to `src/core/virtual_dirs/__init__.py`:

```python
"""Virtual directory providers. See docs/features/virtual_directories.md."""

import logging
from typing import Any

from .single_file import SingleFileProvider
from .tools_provider import ToolsProvider

logger = logging.getLogger(__name__)

# First line written by DescriptionManager.generate_tool_index(). Used as the
# marker that a real tools/ directory is a leftover from materialization and
# not a directory the user owns.
_GENERATED_TOOLS_MARKER = "# Available Tools"


def sweep_legacy_tools_dir(backend: Any) -> bool:
    """Delete a leftover materialized ``tools/`` directory. Never raises.

    Old workspace snapshots still carry the generated docs. Serving them is
    impossible (the prefix is virtual now), but the shell would still show
    stale files, so converge them. A ``tools/`` directory the user created is
    left alone: only the generated marker authorises deletion.

    Returns:
        True when a generated directory was deleted.
    """
    try:
        if not backend.is_dir("tools"):
            return False
        readme = backend.read_file("tools/README.md")
        if not str(readme).lstrip().startswith(_GENERATED_TOOLS_MARKER):
            logger.info("Leaving user-owned tools/ directory in place")
            return False
        backend.delete_directory("tools")
        logger.info("Swept leftover materialized tools/ directory")
        return True
    except FileNotFoundError:
        return False
    except Exception as e:  # never block boot
        logger.warning("tools/ sweep failed (non-fatal): %s", e)
        return False


__all__ = ["SingleFileProvider", "ToolsProvider", "sweep_legacy_tools_dir"]
```

**3b.** In `src/core/workspace.py`, replace `self._backend = backend` (line ~301) with:

```python
        # Virtual directories: wrap the real backend so registered prefixes
        # (tools/, contacts/, instructions.md) are served from live state.
        # See docs/features/virtual_directories.md.
        self._virtual_overlay: Optional["VirtualOverlayBackend"] = None
        if backend is not None and os.getenv(
            "VIRTUAL_DIRS_ENABLED", "true"
        ).lower() not in ("false", "0", "no"):
            from .backends.overlay import VirtualOverlayBackend

            self._virtual_overlay = VirtualOverlayBackend(backend)
            self._backend = self._virtual_overlay
        else:
            self._backend = backend
```

Add near the other properties:

```python
    @property
    def virtual_overlay(self):
        """The virtual overlay, or None when VIRTUAL_DIRS_ENABLED is off."""
        return self._virtual_overlay

    def register_virtual_provider(self, provider) -> None:
        """Register a virtual directory provider. No-op when disabled."""
        if self._virtual_overlay is None:
            logger.debug(
                "VIRTUAL_DIRS_ENABLED is off — ignoring provider %s",
                getattr(provider, "prefix", "?"),
            )
            return
        self._virtual_overlay.register(provider)
```

(Ensure `import os` and `Optional` are already imported in that module; both are.)

**3c.** In `src/agent.py`, replace the tool-doc generation block (the `tools_dir = self._workspace_manager.get_path("tools")` through the `generate_workspace_tool_docs(...)` call, ~2854-2863) with:

```python
        # Tool docs are a virtual directory (docs/features/virtual_directories.md):
        # served from the live tool list, never written to the workspace.
        from .core.virtual_dirs import ToolsProvider, sweep_legacy_tools_dir

        # CRITICAL: hold the PRE-override tool objects. Further down,
        # `self._tools = apply_description_overrides(self._tools)` rebinds the
        # attribute to copies whose deferred-tool descriptions are short
        # blurbs. A provider reading `self._tools` at call time would render
        # those blurbs into tools/<name>.md and defeat the whole deferred-tool
        # design (short in context, FULL on disk). apply_description_overrides
        # returns copies, so the originals this list holds stay full.
        self._full_description_tools = self._tools
        self._workspace_manager.register_virtual_provider(
            ToolsProvider(lambda: self._full_description_tools)
        )
        if self._workspace_manager.virtual_overlay is not None:
            sweep_legacy_tools_dir(self._workspace_manager.virtual_overlay.inner)

        loaded_tool_names = [t.name for t in self._tools]
```

Any path that changes the tool set (the virtual→sandbox upgrade re-derive) re-runs this setup and reassigns `self._full_description_tools`, so live-list freshness is preserved.

Remove the now-unused `generate_workspace_tool_docs` name from the import block at `src/agent.py:60`.

**3d.** In `src/api/persistent_session.py`, replace the `try:` block at 1606-1621 with:

```python
        # Tool docs are virtual (docs/features/virtual_directories.md).
        from ..core.virtual_dirs import ToolsProvider, sweep_legacy_tools_dir

        # Pre-override objects — see the CRITICAL note in the agent.py step:
        # `self.tools = apply_description_overrides(self.tools)` below rebinds
        # the attribute to short-description copies.
        self._full_description_tools = self.tools
        self.workspace_manager.register_virtual_provider(
            ToolsProvider(lambda: self._full_description_tools)
        )
        if self.workspace_manager.virtual_overlay is not None:
            sweep_legacy_tools_dir(self.workspace_manager.virtual_overlay.inner)
```

**3e.** In `src/tools/delegation/reader_env.py`, after `reader_ws = WorkspaceManager(...)` and after the reader's tools are loaded, register a reader-scoped provider so a reader's `tools/` describes *its* tool set (readers get their own `WorkspaceManager` over a `SubdirBackend`, so they do not inherit the parent's providers):

```python
    # Readers get their own overlay (own WorkspaceManager); give it a provider
    # bound to the reader's own tool list.
    from src.core.virtual_dirs import ToolsProvider

    reader_ws.register_virtual_provider(ToolsProvider(lambda: reader_tools))
```

Place this immediately after the statement that binds the reader's loaded tools; if that local is named differently, use that name — the provider must read the reader's list, not the parent's.

**3f.** In `src/tools/description_manager.py`, delete `DescriptionManager.generate_workspace_docs` and the module-level `generate_workspace_tool_docs` wrapper. Keep `generate_tool_index`, `generate_tool_description`, `extract_docstrings`, `apply_description_overrides`, `get_deferred_tools`, `get_core_tools`. Remove `Callable`/`Path` imports if they become unused. In `src/tools/__init__.py`, remove `generate_workspace_tool_docs` from both the import block (line ~44) and `__all__` (line ~120).

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_virtual_dirs_wiring.py tests/test_virtual_overlay.py tests/test_virtual_providers.py -q`
Expected: PASS

Then confirm nothing still calls the deleted helpers:

Run: `rg -n "generate_workspace_tool_docs|generate_workspace_docs" src/ tests/`
Expected: no matches

- [ ] **Step 5: Commit**

```bash
git add src/core/workspace.py src/core/virtual_dirs src/agent.py \
  src/api/persistent_session.py src/tools/delegation/reader_env.py \
  src/tools/description_manager.py src/tools/__init__.py \
  tests/test_virtual_dirs_wiring.py
git commit -m "feat(workspace): serve tools/ virtually and delete the materialization path"
```

---

### Task 6: InstructionsProvider — `instructions.md` and `task_brief.md`

**Files:**
- Modify: `src/agent.py` (`_deploy_instruction_files` ~3118-3126; the resume repair path ~2164-2170; the task-brief write ~2279-2288; the upload branch ~2213-2277)
- Modify: `src/api/persistent_session.py:995` (`_deploy_instruction_files`)
- Modify: `src/core/virtual_dirs/__init__.py` (export a builder)
- Test: `tests/test_virtual_providers.py`

**Interfaces:**
- Consumes: `SingleFileProvider` (Task 4); `load_instructions(config, model)` and `render_instruction_content(content, loaded_tool_names)` from `src/core/loader.py`
- Produces: `build_instruction_providers(*, uploaded: Callable[[], str | None], template: Callable[[], str], brief: Callable[[], str]) -> list` returning providers for `instructions.md` and `task_brief.md`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_virtual_providers.py
from src.core.virtual_dirs import build_instruction_providers


def _providers(uploaded=None, template="TEMPLATE", brief="# Task Brief\n"):
    return {
        p.prefix: p
        for p in build_instruction_providers(
            uploaded=lambda: uploaded,
            template=lambda: template,
            brief=lambda: brief,
        )
    }


def test_builds_both_instruction_files():
    assert set(_providers()) == {"instructions.md", "task_brief.md"}


def test_uploaded_instructions_beat_the_template():
    provider = _providers(uploaded="UPLOADED")["instructions.md"]
    assert provider.read("instructions.md") == "UPLOADED"


def test_template_is_used_when_no_upload():
    provider = _providers(uploaded=None)["instructions.md"]
    assert provider.read("instructions.md") == "TEMPLATE"


def test_blank_upload_falls_back_to_template():
    provider = _providers(uploaded="   ")["instructions.md"]
    assert provider.read("instructions.md") == "TEMPLATE"


def test_task_brief_is_served_from_the_callable():
    provider = _providers(brief="# Task Brief\n\nDo the thing.")["task_brief.md"]
    assert "Do the thing." in provider.read("task_brief.md")


def test_instruction_providers_are_read_only():
    for provider in _providers().values():
        assert not provider.writable and not provider.is_dir
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_virtual_providers.py -x -q -k instruction`
Expected: FAIL — `ImportError: cannot import name 'build_instruction_providers'`

- [ ] **Step 3: Write minimal implementation**

**3a.** Add to `src/core/virtual_dirs/__init__.py` (and to `__all__`):

```python
def build_instruction_providers(*, uploaded, template, brief) -> list:
    """Providers for instructions.md and task_brief.md.

    Precedence for instructions.md is resolved here, in one place: an upload or
    inline body from the job record wins; otherwise the rendered template. The
    materialized version resolved this with exists() probes across three call
    sites, which is how a remote-backend probe once clobbered user-provided
    instructions with the template (src/agent.py, 2026-07 comment).
    """

    def _instructions() -> str:
        body = uploaded()
        if body and body.strip():
            return body
        return template()

    return [
        SingleFileProvider("instructions.md", _instructions),
        SingleFileProvider("task_brief.md", brief),
    ]
```

**3b.** In `src/agent.py._deploy_instruction_files`, delete the `instructions.md` block (the `if not self._workspace_manager.exists("instructions.md"):` guard and its body, ~3118-3126) and register providers instead. `_deploy_instruction_files` already receives `loaded_tool_names`, which the template render needs:

```python
        # instructions.md / task_brief.md are virtual
        # (docs/features/virtual_directories.md): served from the job record or
        # the rendered template, never written to the workspace. This deletes
        # the exists()-probe precedence dance and the "rewrite if it vanished"
        # repair path — a virtual file cannot go missing.
        from .core.virtual_dirs import build_instruction_providers

        metadata = self._job_metadata or {}

        def _uploaded_instructions():
            return metadata.get("instructions")

        def _rendered_template():
            content = load_instructions(self.config, model=self.config.llm.model)
            return render_instruction_content(content, loaded_tool_names)

        def _task_brief():
            description = metadata.get("description", "")
            kickoff_message = metadata.get("kickoff_message", "")
            parts = [f"# Task Brief\n\n## Description\n\n{description}"]
            if kickoff_message:
                parts.append(f"\n\n## Kickoff Message\n\n{kickoff_message}")
            return "".join(parts)

        for provider in build_instruction_providers(
            uploaded=_uploaded_instructions,
            template=_rendered_template,
            brief=_task_brief,
        ):
            self._workspace_manager.register_virtual_provider(provider)
```

Use whatever attribute already holds the job metadata dict in that class for `metadata` — the same dict the deleted `write_file("task_brief.md", ...)` at ~2280 read `description` / `kickoff_message` from. If it is a local in another method, hoist it to an instance attribute set before `_deploy_instruction_files` runs.

**3c.** Delete the superseded writes and repair paths in `src/agent.py`:
- the `task_brief.md` write and its `_agent_seed_files["task_brief.md"] = brief_content` line (~2285-2288),
- the instructions upload/inline `write_file("instructions.md", ...)` calls (~2213, ~2239, ~2259) and the `instructions_written` bookkeeping — the provider now reads `metadata["instructions"]` directly; keep any code that *populates* that metadata from an upload,
- the resume repair block `if not self._workspace_manager.exists("instructions.md"): ... write_file(...)` (~2164-2170).

**3d.** Apply the same replacement in `src/api/persistent_session.py._deploy_instruction_files` (line ~995): register the providers instead of writing `instructions.md`. Sessions have no task brief; pass `brief=lambda: ""` only if the session path currently writes one — otherwise register just the instructions provider by selecting it from the returned list.

**3e.** Config-driven `instruction_files` (literal files and bound skills → `skills/<name>/SKILL.md`) stay exactly as they are: real files, still written, still tracked in `_agent_seed_files`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_virtual_providers.py -q`
Expected: PASS

Run: `rg -n 'write_file\("(instructions|task_brief)\.md"' src/`
Expected: no matches

- [ ] **Step 5: Commit**

```bash
git add src/core/virtual_dirs/__init__.py src/agent.py \
  src/api/persistent_session.py tests/test_virtual_providers.py
git commit -m "feat(workspace): serve instructions.md and task_brief.md virtually"
```

---

### Task 7: Retarget the `task_brief.md` sentinel probes

**Files:**
- Modify: `src/agent.py:2011` (VM snapshot re-seed probe), `src/agent.py:2105-2123` (`_backend_has`), `src/agent.py:2159` (resume probe)
- Test: `tests/test_virtual_dirs_wiring.py`

**Interfaces:**
- Consumes: `VirtualOverlayBackend.inner` (Task 1)
- Produces: `unwrap_backend(backend) -> Any` in `src/core/virtual_dirs/__init__.py` — returns `backend.inner` for an overlay, otherwise `backend` unchanged

**Why this task exists:** `task_brief.md`'s *existence* is the proxy for "this workspace still has its seeded content" (`if resume and _backend_has("task_brief.md")`). Once it is virtual it always exists, so a freshly-wiped pod would classify as seeded and skip re-seeding — reopening the unseeded-workspace failure. Every such probe must ask the **inner** backend.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_virtual_dirs_wiring.py
from src.core.virtual_dirs import SingleFileProvider, unwrap_backend


def test_unwrap_returns_inner_for_overlay(tmp_path):
    inner = FilesystemTestBackend(tmp_path)
    assert unwrap_backend(VirtualOverlayBackend(inner)) is inner


def test_unwrap_passes_plain_backends_through(tmp_path):
    backend = FilesystemTestBackend(tmp_path)
    assert unwrap_backend(backend) is backend


def test_virtual_task_brief_must_not_mask_an_unseeded_workspace(tmp_path):
    """Regression: the seeded-content probe must see the real filesystem."""
    overlay = VirtualOverlayBackend(FilesystemTestBackend(tmp_path))
    overlay.register(SingleFileProvider("task_brief.md", lambda: "# Task Brief\n"))

    # Virtually present ...
    assert overlay.exists("task_brief.md")
    # ... but the workspace is empty, so the probe must report unseeded.
    assert not unwrap_backend(overlay).exists("task_brief.md")


def test_probe_sees_a_genuinely_seeded_workspace(tmp_path):
    overlay = VirtualOverlayBackend(FilesystemTestBackend(tmp_path))
    overlay.register(SingleFileProvider("task_brief.md", lambda: "# Task Brief\n"))
    (tmp_path / "task_brief.md").write_text("seeded earlier")
    assert unwrap_backend(overlay).exists("task_brief.md")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_virtual_dirs_wiring.py -x -q -k "unwrap or unseeded or seeded"`
Expected: FAIL — `ImportError: cannot import name 'unwrap_backend'`

- [ ] **Step 3: Write minimal implementation**

**3a.** Add to `src/core/virtual_dirs/__init__.py` (and `__all__`):

```python
def unwrap_backend(backend: Any) -> Any:
    """Return the real backend behind a virtual overlay.

    Content probes that ask "does this workspace still hold its seeded files?"
    must bypass virtualization: a virtual task_brief.md always exists, so a
    naive probe would classify a wiped workspace as seeded and skip re-seeding.
    """
    return getattr(backend, "inner", backend)
```

**3b.** In `src/agent.py`, at the VM snapshot re-seed probe (~2011):

```python
                from .core.virtual_dirs import unwrap_backend

                if not unwrap_backend(workspace_backend).exists("task_brief.md"):
```

**3c.** In `src/agent.py._backend_has` (~2105), probe the unwrapped backend and extend the docstring:

```python
        def _backend_has(rel: str) -> bool:
            """Probe the REAL workspace backend, treating failures as absent.

            Bypasses the virtual overlay on purpose: instructions.md and
            task_brief.md are virtual and always "exist", so probing through
            the overlay would report every fresh pod as seeded. The question
            here is strictly "did real seeded content survive?".
            """
            from .core.virtual_dirs import unwrap_backend

            probe = unwrap_backend(self._workspace_manager.backend)
```

Keep the rest of the existing body, but call `probe.exists(rel)` instead of the previous backend reference. The `.git` probe at ~2123 inherits the fix for free (`.git` is not virtual, so behavior is unchanged).

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_virtual_dirs_wiring.py -q`
Expected: PASS

Confirm no probe still asks the overlay:

Run: `rg -n 'exists\("task_brief.md"\)' src/`
Expected: every hit goes through `unwrap_backend(...)`

- [ ] **Step 5: Commit**

```bash
git add src/core/virtual_dirs/__init__.py src/agent.py tests/test_virtual_dirs_wiring.py
git commit -m "fix(workspace): retarget seeded-content probes at the real backend"
```

---

### Task 8: ContactsProvider and the internal contacts endpoint

**Files:**
- Create: `src/core/virtual_dirs/contacts_provider.py`
- Modify: `orchestrator/routers/contacts.py`
- Modify: `src/agent.py`, `src/api/persistent_session.py` (registration)
- Test: `tests/test_virtual_providers.py`, `tests/test_contacts_internal_endpoint.py`

**Interfaces:**
- Consumes: `EntryMeta` (Task 1); `contact_slug(display_name, taken)`, `render_contact_md(contact)` from `src/core/contact_files.py`; `require_internal` from `orchestrator/security/access.py:1120`
- Produces: `ContactsProvider(fetch: Callable[[], list[dict]], ttl_seconds: float = 60.0)` with `prefix="contacts"`, `is_dir=True`; `GET /api/internal/contacts?job_id=…|thread_id=…` returning `{"contacts": [...]}`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_virtual_providers.py
import pytest

from src.core.virtual_dirs import ContactsProvider

CONTACTS = [
    {
        "display_name": "Anna Weber",
        "notes": "Head of Operations.",
        "addresses": [{"channel": "email", "address": "anna@acme.de", "is_primary": True}],
        "projects": [{"name": "Acme Website"}],
    }
]


def test_contacts_entries_include_index_and_each_contact():
    provider = ContactsProvider(lambda: CONTACTS)
    assert set(provider.entries()) == {"README.md", "anna-weber.md"}


def test_contact_file_carries_name_and_notes():
    body = ContactsProvider(lambda: CONTACTS).read("anna-weber.md")
    assert "Anna Weber" in body and "Head of Operations." in body


def test_readme_lists_contacts():
    assert "Anna Weber" in ContactsProvider(lambda: CONTACTS).read("README.md")


def test_slug_collisions_are_deterministic():
    duplicates = [dict(CONTACTS[0]), dict(CONTACTS[0])]
    names = set(ContactsProvider(lambda: duplicates).entries())
    assert {"anna-weber.md", "anna-weber-2.md"} <= names


def test_empty_project_renders_an_empty_index():
    provider = ContactsProvider(lambda: [])
    assert set(provider.entries()) == {"README.md"}
    assert "no contacts" in provider.read("README.md").lower()


def test_fetch_happens_once_per_ttl_window():
    calls = []

    def fetch():
        calls.append(1)
        return CONTACTS

    provider = ContactsProvider(fetch, ttl_seconds=3600)
    provider.entries()
    provider.read("anna-weber.md")
    assert len(calls) == 1


def test_stale_cache_is_served_when_the_fetch_fails():
    state = {"fail": False}

    def fetch():
        if state["fail"]:
            raise RuntimeError("orchestrator down")
        return CONTACTS

    provider = ContactsProvider(fetch, ttl_seconds=0)
    assert provider.read("anna-weber.md")
    state["fail"] = True
    assert "Anna Weber" in provider.read("anna-weber.md")  # stale, not an error


def test_error_when_the_fetch_fails_with_a_cold_cache():
    def fetch():
        raise RuntimeError("orchestrator down")

    with pytest.raises(ValueError, match="temporarily unavailable"):
        ContactsProvider(fetch).entries()
```

Note the house import style: `tests/conftest.py` puts `orchestrator/` on `sys.path`, so router tests import `from routers import ...` (see `tests/test_contacts_api.py`), never `from orchestrator.routers import ...`.

```python
# tests/test_contacts_internal_endpoint.py
import pytest
from fastapi import HTTPException

from routers import contacts as contacts_router


@pytest.mark.asyncio
async def test_internal_contacts_requires_the_internal_key(monkeypatch):
    """No X-Internal-Key -> 401, regardless of query parameters."""

    async def deny(request):
        raise HTTPException(status_code=401, detail="Invalid internal key")

    monkeypatch.setattr(contacts_router, "require_internal", deny)
    with pytest.raises(HTTPException) as excinfo:
        await contacts_router.list_internal_contacts(
            request=object(), job_id="job-1", thread_id=None
        )
    assert excinfo.value.status_code == 401


@pytest.mark.asyncio
async def test_requires_exactly_one_of_job_or_thread(monkeypatch):
    async def allow(request):
        return None

    monkeypatch.setattr(contacts_router, "require_internal", allow)
    with pytest.raises(HTTPException) as excinfo:
        await contacts_router.list_internal_contacts(
            request=object(), job_id=None, thread_id=None
        )
    assert excinfo.value.status_code == 400
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_virtual_providers.py -x -q -k contact`
Expected: FAIL — `ImportError: cannot import name 'ContactsProvider'`

- [ ] **Step 3: Write minimal implementation**

**3a.** `src/core/virtual_dirs/contacts_provider.py`:

```python
"""ContactsProvider — contacts/ served live from the orchestrator.

The DB is the source of truth (docs/features/contacts_registry.md). The agent
sees a read-only projection with a short TTL, so a contact linked mid-session
becomes visible without a restart. Deliberately not the reverted boot-snapshot
approach (commit b8e48c10), which could only ever be as fresh as job start.
"""

import logging
import time
from typing import Callable, Dict, List, Optional

from ..backends.overlay import EntryMeta
from ..contact_files import contact_slug, render_contact_md

logger = logging.getLogger(__name__)


class ContactsProvider:
    prefix = "contacts"
    is_dir = True
    writable = False

    def __init__(self, fetch: Callable[[], List[dict]], ttl_seconds: float = 60.0):
        self._fetch = fetch
        self._ttl = ttl_seconds
        self._cache: Optional[Dict[str, str]] = None
        self._fetched_at = 0.0

    def _render(self, contacts: List[dict]) -> Dict[str, str]:
        docs: Dict[str, str] = {}
        taken: set = set()
        lines = ["# Contacts", ""]
        if not contacts:
            lines.append("No contacts are linked to this project.")
        for contact in contacts:
            slug = contact_slug(contact.get("display_name", "contact"), taken)
            taken.add(slug)
            docs[f"{slug}.md"] = render_contact_md(contact)
            channels = sorted(
                {a.get("channel", "") for a in contact.get("addresses", []) if a}
            )
            suffix = f" — {', '.join(channels)}" if channels else ""
            lines.append(f"- [{contact.get('display_name', slug)}]({slug}.md){suffix}")
        lines.append("")
        docs["README.md"] = "\n".join(lines)
        return docs

    def _docs(self) -> Dict[str, str]:
        fresh = self._cache is not None and (time.monotonic() - self._fetched_at) < self._ttl
        if fresh:
            return self._cache
        try:
            self._cache = self._render(self._fetch() or [])
            self._fetched_at = time.monotonic()
        except Exception as e:
            if self._cache is not None:
                logger.warning("contacts fetch failed; serving stale cache: %s", e)
                return self._cache
            logger.warning("contacts fetch failed with a cold cache: %s", e)
            raise ValueError(f"contacts are temporarily unavailable: {e}") from e
        return self._cache

    def entries(self) -> Dict[str, EntryMeta]:
        return {
            name: EntryMeta(size=len(body.encode("utf-8")))
            for name, body in self._docs().items()
        }

    def read(self, name: str) -> Optional[str]:
        return self._docs().get(name)
```

Export it from `src/core/virtual_dirs/__init__.py` (import + `__all__`).

**3b.** Add the internal endpoint to `orchestrator/routers/contacts.py`. The module already imports its guards as `from security.access import require_project_member` (orchestrator's own root is on `sys.path`) — add `require_internal` to that import. The DB handle comes from the module's `_get_db()` helper, **not** a module-level `postgres_db`. The router's prefix is `/api/contacts`, so declare this route **above** the parameterized routes:

```python
@router.get("/internal/list")
async def list_internal_contacts(
    request: Request,
    job_id: str | None = None,
    thread_id: str | None = None,
) -> dict:
    """Contacts linked to the caller's project. Agent-internal.

    The project is derived server-side from the job/thread — the agent never
    supplies a project_id, so it cannot read another project's contacts. Same
    trust posture as send_message recipient resolution.
    """
    await require_internal(request)
    if bool(job_id) == bool(thread_id):
        raise HTTPException(
            status_code=400, detail="Provide exactly one of job_id or thread_id"
        )
    db = _get_db()
    project_id = await db.resolve_project_for_agent(
        job_id=job_id, thread_id=thread_id
    )
    if not project_id:
        return {"contacts": []}
    return {"contacts": await db.get_project_contacts(project_id)}
```

Register the route so it is reachable under the app's internal surface, following whatever prefix the existing `contacts` router uses. `get_project_contacts(project_id)` already exists from the contacts implementation; add `resolve_project_for_agent(job_id, thread_id)` to `orchestrator/database/postgres.py` as a single query returning `jobs.project_id` or `threads.project_id`.

**3c.** Register the provider at both agent boot paths, next to the tools provider, using a **sync** `httpx` client (the backend API is synchronous; the async `OrchestratorClient` cannot be awaited from it):

```python
        # contacts/ is virtual and project-scoped (docs/features/contacts_registry.md).
        import os

        import httpx

        from .core.virtual_dirs import ContactsProvider

        orchestrator_url = os.getenv("ORCHESTRATOR_URL", "").rstrip("/")
        if orchestrator_url and job_id:

            def _fetch_contacts():
                response = httpx.get(
                    f"{orchestrator_url}/api/contacts/internal/list",
                    params={"job_id": job_id},
                    headers={"X-Internal-Key": os.getenv("MCP_INTERNAL_KEY", "")},
                    timeout=3.0,
                )
                response.raise_for_status()
                return response.json().get("contacts", [])

            self._workspace_manager.register_virtual_provider(
                ContactsProvider(_fetch_contacts)
            )
```

In `src/api/persistent_session.py` register the same provider with `params={"thread_id": thread_id}`. Skip registration when the job/session has no project — the path then falls through to the real filesystem like any other.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_virtual_providers.py tests/test_contacts_internal_endpoint.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/core/virtual_dirs/contacts_provider.py src/core/virtual_dirs/__init__.py \
  orchestrator/routers/contacts.py orchestrator/database/postgres.py \
  src/agent.py src/api/persistent_session.py \
  tests/test_virtual_providers.py tests/test_contacts_internal_endpoint.py
git commit -m "feat(contacts): serve contacts/ as a live virtual directory"
```

---

### Task 9: Full suite, then live gate on local k3d

**Files:**
- Modify: `docs/features/virtual_directories.md` (status line only)

**Interfaces:**
- Consumes: everything above
- Produces: a verified deployment and an updated spec status

- [ ] **Step 1: Run the full test suite**

Run: `python -m pytest tests/ -q 2>&1 | tail -30`
Expected: no failures attributable to this work. Local pytest is noisy on Python 3.14 — compare against a pre-change run and treat CI (3.12) as the gate.

- [ ] **Step 2: Check for stragglers**

Run: `rg -n "generate_workspace_tool_docs|CONTACTS_MATERIALIZE_ENABLED" src/ orchestrator/ tests/`
Expected: no matches

- [ ] **Step 3: Build and deploy to local k3d**

Follow the house local-stack flow (`k3d` cluster `srw` + `tilt up`, registry on 5005). Confirm the agent image contains `src/core/virtual_dirs/` and that `VIRTUAL_DIRS_ENABLED` is unset or `true`.

- [ ] **Step 4: Run the live gate**

In a session on the deployed stack, verify each and record the output:

1. `list_files` at the workspace root shows `tools/`, `instructions.md`, `task_brief.md` (and `contacts/` when the project has contacts).
2. `read_file("tools/README.md")` returns the tool index; `read_file("tools/<a deferred tool>.md")` returns its full docstring.
3. `write_file("tools/x.md", "…")` fails with the teaching error naming the copy-out escape hatch.
4. `copy_file("tools/README.md", "my_tools.md")` succeeds and produces a real, editable file.
5. `search_files("read_file")` returns hits from both real files and `tools/`.
6. `run_command("ls")` does **not** list `tools/` — virtual paths are invisible to the shell, as designed.
7. Link a contact to the project mid-session; within ~60s `read_file("contacts/<slug>.md")` shows it without a restart.
8. Restart the workspace pod, then confirm `instructions.md` still reads correctly (it cannot go missing) and that a genuinely wiped workspace still triggers re-seeding rather than being treated as seeded.

This session also discharges the contacts registry's own never-run live gate (`docs/features/contacts_registry.md` records it as outstanding): exercise `send_message` to a project contact while you are there.

- [ ] **Step 5: Update the spec status and commit**

Change the `**Status:**` line in `docs/features/virtual_directories.md` to record Slice 1 as implemented and live-gated, with the date and the k3d gate result. Leave Slice 2 marked as not started.

```bash
git add docs/features/virtual_directories.md
git commit -m "docs(specs): record virtual directories Slice 1 as implemented and live-gated"
```

---

## Self-Review

**Spec coverage:** Overlay seam and `SubdirBackend` idiom → Task 1. Provider contract incl. `writable`/`write()` → Tasks 1, 3. Flat trees → Task 2 (`list_dir` returns `[]` below a prefix). Full subtree ownership → Task 1. Listings merge/dedupe → Task 2. Search merge + hard cap → Task 2. Mutation rules + copy-out → Task 3. ToolsProvider → Task 4. Wiring + kill switch + write-path deletion + `tools/` sweep → Task 5. Subagent readers → Task 5, step 3e. InstructionsProvider + deleted repair paths → Task 6. Sentinel probes → Task 7. ContactsProvider + internal endpoint → Task 8. Live gate → Task 9.

**Known deviations from the spec, deliberate:**
- `resolve_path` is *not* overridden. Delegating is already correct — it returns a canonical string and validates boundaries, neither of which changes for a virtual path. The spec lists it among overridden methods; the smaller surface is better and the plan is the contract.
- Reader subagents are handled by explicit registration in `reader_env.py` (Task 5, step 3e), not by "inheriting through the shared context" as the spec's Architecture section claims. Readers construct their own `WorkspaceManager` over a `SubdirBackend`, so they get their own overlay. **The spec's sentence should be corrected when Slice 1 lands.**
- The `instructions.md` deletion in Task 6 assumes the job-metadata dict is reachable from `_deploy_instruction_files`. If it is not, hoist it to an instance attribute (called out in step 3b) rather than re-reading the upload from disk.

**Out of scope (Slice 2, separate plan):** `job_documents` table, `plan.md` / `todos.yaml` providers, write-through to Postgres, mtime shadow reconciliation, rewiring `orchestrator/services/workspace.py` display paths, checkpoint-restore reconciliation.
