"""Tests for ScratchBackend (the ``none``-mode disposable tmpdir backend).

ScratchBackend is the filesystem backend the ``none`` tier hands to the
graph's internal consumers (PlanManager, TodoManager archive) — no file tools
are registered over it. These tests pin the file contract plus the two things
that make it production-safe: it owns + cleans up its own private tmpdir, and
it declares no shell capability.
"""

import sys
from pathlib import Path

import pytest

project_root = Path(__file__).parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from src.core.backends.scratch import ScratchBackend  # noqa: E402
from src.core.workspace_backend import WorkspaceBackend  # noqa: E402


@pytest.fixture
def backend(tmp_path):
    b = ScratchBackend(job_id="job-abc", base_dir=str(tmp_path))
    yield b
    b.disconnect()


class TestLifecycleAndIsolation:
    def test_is_a_workspace_backend(self, backend):
        assert isinstance(backend, WorkspaceBackend)

    def test_creates_owned_tmpdir(self, backend, tmp_path):
        root = Path(backend.root)
        assert root.exists() and root.is_dir()
        assert str(root).startswith(str(tmp_path))
        assert "srw-scratch-job-abc" in root.name

    def test_supports_shell_is_false(self, backend):
        assert backend.supports_shell is False

    def test_disconnect_removes_tmpdir(self, tmp_path):
        b = ScratchBackend(base_dir=str(tmp_path))
        root = Path(b.root)
        b.write_file("f.txt", "x")
        assert root.exists()
        b.disconnect()
        assert not root.exists()
        assert b.is_connected() is False

    def test_distinct_instances_isolated(self, tmp_path):
        a = ScratchBackend(base_dir=str(tmp_path))
        b = ScratchBackend(base_dir=str(tmp_path))
        try:
            a.write_file("secret.txt", "a-only")
            assert b.exists("secret.txt") is False
            assert a.root != b.root
        finally:
            a.disconnect()
            b.disconnect()


class TestFileContract:
    def test_write_read_roundtrip(self, backend):
        backend.write_file("notes.md", "hello")
        assert backend.read_file("notes.md") == "hello"

    def test_binary_roundtrip(self, backend):
        backend.write_file("b.bin", b"\x00\x01")
        assert backend.read_file("b.bin", binary=True) == b"\x00\x01"

    def test_write_creates_parent_dirs(self, backend):
        backend.write_file("a/b/c.txt", "x")
        assert backend.read_file("a/b/c.txt") == "x"

    def test_append(self, backend):
        backend.write_file("log.txt", "a\n")
        backend.append_file("log.txt", "b\n")
        assert backend.read_file("log.txt") == "a\nb\n"

    def test_read_missing_raises(self, backend):
        with pytest.raises(FileNotFoundError):
            backend.read_file("ghost.txt")

    def test_exists_is_file_is_dir(self, backend):
        backend.write_file("f.txt", "x")
        backend.mkdir("d")
        assert backend.is_file("f.txt") is True
        assert backend.is_dir("d") is True
        assert backend.exists("nope") is False

    def test_list_dir(self, backend):
        backend.write_file("a.txt", "x")
        backend.mkdir("sub")
        result = backend.list_dir("")
        assert "a.txt" in result
        assert "sub/" in result

    def test_list_dir_pattern(self, backend):
        backend.write_file("a.py", "")
        backend.write_file("b.txt", "")
        assert backend.list_dir("", pattern="*.py") == ["a.py"]

    def test_search_files(self, backend):
        backend.write_file("a.txt", "find the needle here")
        results = backend.search_files("needle")
        assert len(results) == 1
        assert results[0]["path"] == "a.txt"

    def test_delete_file(self, backend):
        backend.write_file("f.txt", "x")
        assert backend.delete_file("f.txt") is True
        assert backend.exists("f.txt") is False

    def test_delete_directory(self, backend):
        backend.write_file("tree/a.txt", "x")
        assert backend.delete_directory("tree") is True
        assert backend.exists("tree") is False

    def test_move(self, backend):
        backend.write_file("old.txt", "data")
        backend.move("old.txt", "new.txt")
        assert backend.read_file("new.txt") == "data"
        assert backend.exists("old.txt") is False

    def test_copy(self, backend):
        backend.write_file("src.txt", "data")
        backend.copy("src.txt", "dst.txt")
        assert backend.read_file("dst.txt") == "data"
        assert backend.exists("src.txt") is True

    def test_stat(self, backend):
        backend.write_file("f.txt", "12345")
        assert backend.stat("f.txt") == 5

    def test_resolve_path_within_root(self, backend):
        resolved = backend.resolve_path("a/b.txt")
        assert resolved.startswith(backend.root)


class TestBoundary:
    def test_traversal_rejected(self, backend):
        with pytest.raises(ValueError, match="escapes workspace boundary"):
            backend.read_file("../../etc/passwd")

    def test_delete_root_rejected(self, backend):
        with pytest.raises(ValueError, match="workspace root"):
            backend.delete_directory("")


class TestNoHome:
    def test_write_home_file_unsupported(self, backend):
        with pytest.raises(NotImplementedError):
            backend.write_home_file(".ssh/key", "secret")
