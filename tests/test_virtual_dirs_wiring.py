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
    ws.register_virtual_provider(
        ToolsProvider(lambda: [FakeTool("read_file", "Reads.")])
    )
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
