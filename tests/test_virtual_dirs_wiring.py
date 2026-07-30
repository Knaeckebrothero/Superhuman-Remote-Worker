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
