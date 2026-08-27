import pytest

from src.core.backends.overlay import VirtualOverlayBackend
from src.core.virtual_dirs import (
    SingleFileProvider,
    ToolsProvider,
    sweep_legacy_tools_dir,
    unwrap_backend,
)
from src.core.workspace import WorkspaceManager, WorkspaceManagerConfig
from tests._fs_backend import FilesystemTestBackend


class FakeTool:
    def __init__(self, name, description):
        self.name = name
        self.description = description


class CopyableFakeTool:
    """Fake tool supporting ``model_copy``, like the real Pydantic StructuredTool.

    Required: without ``model_copy``, ``_copy_with_description`` falls back to
    mutating the tool **in place** (`description_manager.py`, "Could not copy
    tool" branch), which corrupts the pre-override objects and would fail even
    correct wiring.
    """

    def __init__(self, name, description):
        self.name = name
        self.description = description

    def model_copy(self, update=None):
        clone = CopyableFakeTool(self.name, self.description)
        for key, value in (update or {}).items():
            setattr(clone, key, value)
        return clone


# Distinguishes "caller wants the default backend" from an explicit None,
# which is the only way to get a manager with no overlay.
_UNSET = object()


def _manager(tmp_path, monkeypatch=None, backend=_UNSET):
    """A manager with the overlay, unless ``backend=None`` is asked for.

    There is no enable/disable flag any more — the overlay is unconditional
    when there is a backend to wrap. The only overlay-less manager is a
    backend-less one.
    """
    return WorkspaceManager(
        job_id="job-1",
        config=WorkspaceManagerConfig(base_path=str(tmp_path)),
        backend=FilesystemTestBackend(tmp_path) if backend is _UNSET else backend,
    )


def test_manager_wraps_backend_in_overlay(tmp_path, monkeypatch):
    ws = _manager(tmp_path, monkeypatch)
    assert isinstance(ws.backend, VirtualOverlayBackend)


def test_virtual_dirs_enabled_is_inert(tmp_path, monkeypatch):
    """The kill switch is gone; setting it must not bring the overlay down.

    Its "off" position materialized instructions.md / task_brief.md into the
    workspace root, which on a workspace-inheriting subjob put the critic's
    brief where the target reads it. Pinned here so a reintroduced flag fails
    loudly instead of quietly restoring that write path.
    """
    monkeypatch.setenv("VIRTUAL_DIRS_ENABLED", "false")
    ws = _manager(tmp_path)
    assert isinstance(ws.backend, VirtualOverlayBackend)
    assert ws.virtual_overlay is not None


def test_registered_provider_serves_reads_through_the_manager(tmp_path, monkeypatch):
    ws = _manager(tmp_path, monkeypatch)
    ws.register_virtual_provider(
        ToolsProvider(lambda: [FakeTool("read_file", "Reads.")])
    )
    assert "Reads." in ws.read_file("tools/read_file.md")
    assert "tools/" in ws.backend.list_dir("")


def test_every_manager_has_an_overlay(tmp_path):
    """There is no way to construct a manager without one.

    ``backend=None`` raises in the constructor, and nothing reassigns
    ``_virtual_overlay`` afterwards, so ``virtual_overlay is None`` is not a
    reachable state. The guards that used to handle it are gone; this pins the
    invariant they were guarding.
    """
    with pytest.raises(TypeError, match="requires a backend"):
        _manager(tmp_path, backend=None)

    ws = _manager(tmp_path)
    assert ws.virtual_overlay is not None


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
    rebound attribute would serve blurbs — defeating the deferred-tool design
    (short in context, full on disk). The provider must hold the pre-override
    objects.

    The fixture MUST be a real ``defer_to_workspace: True`` tool. With a
    non-deferred tool (e.g. read_file) apply_description_overrides takes its
    pass-through branch, no copy is ever made, and the test passes under the
    buggy binding too — a placebo.
    """
    from src.tools.description_manager import apply_description_overrides

    full_doc = "Full docstring. Every argument explained at length."
    full_tools = [CopyableFakeTool("get_document_info", full_doc)]

    ws = _manager(tmp_path, monkeypatch)
    # The wiring contract under test: bound to the PRE-override objects.
    ws.register_virtual_provider(ToolsProvider(lambda: full_tools))

    rebound = apply_description_overrides(list(full_tools))

    # Guards: prove the override actually shortened something and left the
    # originals intact. Without these the test can silently go vacuous again.
    assert rebound[0].description != full_doc
    assert full_tools[0].description == full_doc

    assert full_doc in ws.read_file("tools/get_document_info.md")


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


def test_swap_backend_keeps_the_overlay_in_front_of_the_new_backend(
    tmp_path, monkeypatch
):
    """Regression: a workspace-tier upgrade must not destroy the overlay.

    ``workspace_manager._backend = new_backend`` replaces the overlay with the
    raw backend and leaves ``_virtual_overlay`` pointing at the old,
    disconnected one — so after a virtual->sandbox upgrade every virtual path
    404s, including the deferred-tool full docs that are the whole point of
    ``defer_to_workspace``. Re-registering a provider cannot repair it: the
    provider lands on an overlay nothing reads through any more.
    """
    ws = _manager(tmp_path, monkeypatch)
    ws.register_virtual_provider(
        ToolsProvider(lambda: [FakeTool("read_file", "Reads.")])
    )

    new_root = tmp_path / "upgraded"
    new_root.mkdir()
    new_backend = FilesystemTestBackend(new_root)
    ws.swap_backend(new_backend)

    assert ws.virtual_overlay is not None
    assert ws.virtual_overlay.inner is new_backend
    assert ws.backend is ws.virtual_overlay
    # The already-registered provider still serves, with no re-registration.
    assert "Reads." in ws.read_file("tools/read_file.md")
    # ... and real reads land on the NEW backend.
    (new_root / "notes.md").write_text("upgraded")
    assert ws.read_file("notes.md") == "upgraded"


def test_swap_backend_keeps_serving_through_the_overlay(tmp_path):
    """The overlay-less swap branch is gone; the overlay must survive a swap.

    Replaces test_swap_backend_without_an_overlay_still_swaps, whose state is
    no longer constructible.
    """
    ws = _manager(tmp_path)
    ws.register_virtual_provider(ToolsProvider(lambda: [FakeTool("read_file", "R.")]))

    new_root = tmp_path / "new"
    new_root.mkdir()
    ws.swap_backend(FilesystemTestBackend(new_root))

    assert ws.virtual_overlay is not None
    assert ws.backend is ws.virtual_overlay
    assert "R." in ws.read_file("tools/read_file.md")


def test_swap_backend_does_not_unwrap_a_stand_in_backend(tmp_path, monkeypatch):
    """``getattr(new_backend, "inner", new_backend)`` swaps in a child mock.

    ``unwrap_backend`` is isinstance-typed on ``VirtualOverlayBackend`` for
    exactly this reason (``src/core/backends/overlay.py``): a ``MagicMock``
    auto-creates every attribute, so duck-typing on ``.inner`` silently hands
    back a child mock and the manager stops pointing at the backend the caller
    passed. ``swap_backend`` must use the helper, not ``getattr``.
    """
    from unittest.mock import MagicMock

    ws = _manager(tmp_path, monkeypatch)
    new_backend = MagicMock()
    ws.swap_backend(new_backend)
    assert ws.virtual_overlay is not None
    assert ws.virtual_overlay.inner is new_backend


def test_production_backend_swaps_go_through_swap_backend():
    """Tripwire: no production site may assign ``_backend`` directly.

    Both hot-swap sites (worker tier upgrade, session container->VM swap) used
    to overwrite the private attribute, silently unwrapping the overlay. Every
    other test in the suite passes with that bug present, so pin the call
    sites.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    for rel in ("src/agent.py", "src/api/persistent_session.py"):
        source = (root / rel).read_text(encoding="utf-8")
        assert "workspace_manager._backend = " not in source, rel
        assert ".swap_backend(new_backend)" in source, rel


def test_workspace_module_constructs_when_loaded_outside_its_package(tmp_path):
    """Regression: six test modules load workspace.py via spec_from_file_location.

    That deliberately bypasses ``src/__init__.py`` side effects, so the module
    has no parent package and a function-local ``from .backends.overlay import
    ...`` raises ``ImportError: attempted relative import with no known parent
    package`` inside ``WorkspaceManager.__init__`` — 129 collection errors
    across the suite. Targeted runs import via the package path and never see
    it, so this pins the direct-load context explicitly.
    """
    import importlib.util
    import sys
    from pathlib import Path

    module_path = Path(__file__).resolve().parents[1] / "src" / "core" / "workspace.py"
    spec = importlib.util.spec_from_file_location("direct_load_workspace", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        # Construction — not import — is what trips the relative import.
        ws = module.WorkspaceManager(
            job_id="job-direct",
            config=module.WorkspaceManagerConfig(base_path=str(tmp_path)),
            backend=FilesystemTestBackend(tmp_path),
        )
        assert ws.virtual_overlay is not None
    finally:
        sys.modules.pop(spec.name, None)


def test_production_probe_call_sites_bypass_the_overlay():
    """Tripwire pinning the real call sites, not just the helper.

    ``workspace_is_seeded`` is unit-tested below, but nothing otherwise pins the
    production probes. An edit — or a merge/rebase conflict resolution — that
    reintroduced a direct ``exists()`` probe would let a virtual file mask a
    wiped workspace, and every other test in the suite would still pass. The
    Step 4 grep cannot catch it either: ``_backend_has`` probes through a
    variable, not the literal string.
    """
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "src" / "agent.py").read_text(
        encoding="utf-8"
    )

    # Both seeded-content probes go through the marker helper, which unwraps.
    assert "workspace_is_seeded(workspace_backend)" in source
    assert "workspace_is_seeded(self._workspace_manager.backend)" in source

    # The old sentinel had no writer left, so it answered "unseeded" forever.
    assert 'exists("task_brief.md")' not in source

    # The marker is written on the fresh-init path, or nothing is ever seeded.
    assert "mark_workspace_seeded(self._workspace_manager.backend)" in source

    # The shared _backend_has helper (still used for the .git probe) unwraps.
    start = source.index("def _backend_has(")
    assert "unwrap_backend(" in source[start : start + 1200]


def test_seeded_marker_probe_ignores_a_virtual_file(tmp_path):
    """A virtual file must never make a wiped workspace look seeded."""
    from src.core.backends.seed import SEEDED_MARKER, workspace_is_seeded

    overlay = VirtualOverlayBackend(FilesystemTestBackend(tmp_path))
    overlay.register(SingleFileProvider("task_brief.md", lambda: "# Task Brief\n"))
    overlay.register(SingleFileProvider(SEEDED_MARKER, lambda: "fake"))

    assert overlay.exists("task_brief.md")
    assert overlay.exists(SEEDED_MARKER)
    assert workspace_is_seeded(overlay) is False


def test_seeded_marker_round_trips_through_the_manager(tmp_path, monkeypatch):
    from src.core.backends.seed import (
        SEEDED_MARKER,
        mark_workspace_seeded,
        workspace_is_seeded,
    )

    ws = _manager(tmp_path, monkeypatch)
    assert workspace_is_seeded(ws.backend) is False

    assert mark_workspace_seeded(ws.backend) is True
    assert (tmp_path / SEEDED_MARKER).exists()
    assert workspace_is_seeded(ws.backend) is True

    # Idempotent: a second boot must not re-dirty the file.
    assert mark_workspace_seeded(ws.backend) is False


def test_legacy_workspace_without_a_marker_still_reads_as_seeded(tmp_path):
    """Workspaces seeded before the marker existed must degrade SAFE.

    They carry a real task_brief.md and no marker. Answering "unseeded" there
    would rewind them to the last phase boundary (VM probe) or wipe them
    (resume gate), so the legacy sentinel is still accepted as evidence.
    """
    from src.core.backends.seed import workspace_is_seeded

    backend = FilesystemTestBackend(tmp_path)
    (tmp_path / "task_brief.md").write_text("seeded by a pre-marker release")
    assert workspace_is_seeded(backend) is True


def test_empty_workspace_reads_as_unseeded(tmp_path):
    from src.core.backends.seed import workspace_is_seeded

    assert workspace_is_seeded(FilesystemTestBackend(tmp_path)) is False
