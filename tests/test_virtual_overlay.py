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
