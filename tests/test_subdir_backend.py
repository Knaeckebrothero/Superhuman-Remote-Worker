"""Unit tests for SubdirBackend — the re-rooted workspace-backend view used to
give each light subagent its own worktree over the parent's shared connection.

Verifies input-path prefixing, output-path stripping (list_dir/search_files/
walk), root computation, and that writes land under the subdir (isolation from
the parent root). Uses FilesystemTestBackend as the parent.
"""

from pathlib import Path

from src.core.backends.subdir import SubdirBackend

from tests._fs_backend import FilesystemTestBackend


def _mk(tmp_path: Path):
    root = tmp_path / "workspace"
    root.mkdir()
    parent = FilesystemTestBackend(root)
    parent.mkdir("wt")  # the "worktree" subdir
    return parent, SubdirBackend(parent, "wt"), root


class TestSubdirBackend:
    def test_write_lands_under_subdir(self, tmp_path):
        parent, sub, root = _mk(tmp_path)
        sub.write_file("a.txt", "hello")
        # Physically under wt/, not at the parent root top level.
        assert (root / "wt" / "a.txt").read_text() == "hello"
        assert not (root / "a.txt").exists()
        # Parent sees it at the prefixed path; reader sees it bare.
        assert parent.exists("wt/a.txt")
        assert sub.exists("a.txt")
        assert not parent.exists("a.txt")

    def test_read_roundtrip(self, tmp_path):
        _, sub, _ = _mk(tmp_path)
        sub.write_file("dir/b.txt", "content")
        assert sub.read_file("dir/b.txt") == "content"
        assert sub.is_file("dir/b.txt")
        assert sub.is_dir("dir")

    def test_list_dir_strips_prefix(self, tmp_path):
        _, sub, _ = _mk(tmp_path)
        sub.write_file("a.txt", "x")
        sub.mkdir("sub")
        sub.write_file("sub/b.txt", "y")
        top = sub.list_dir("")
        # Reader-relative, no "wt/" prefix anywhere.
        assert "a.txt" in top
        assert "sub/" in top
        assert all(not e.startswith("wt/") for e in top)
        assert sub.list_dir("sub") == ["sub/b.txt"]

    def test_search_files_strips_path(self, tmp_path):
        _, sub, _ = _mk(tmp_path)
        sub.write_file("notes.md", "find the needle here")
        hits = sub.search_files("needle")
        assert hits and hits[0]["path"] == "notes.md"
        assert all(not h["path"].startswith("wt/") for h in hits)

    def test_walk_strips_prefix(self, tmp_path):
        _, sub, _ = _mk(tmp_path)
        sub.write_file("a.txt", "1")
        sub.mkdir("d")
        sub.write_file("d/c.txt", "2")
        assert sub.walk("") == ["a.txt", "d/c.txt"]

    def test_root_is_parent_root_plus_subdir(self, tmp_path):
        parent, sub, _ = _mk(tmp_path)
        assert sub.root == parent.root.rstrip("/") + "/wt"

    def test_move_and_copy_and_delete(self, tmp_path):
        _, sub, _ = _mk(tmp_path)
        sub.write_file("a.txt", "z")
        sub.copy("a.txt", "b.txt")
        assert sub.read_file("b.txt") == "z"
        sub.move("a.txt", "c.txt")
        assert sub.exists("c.txt")
        assert not sub.exists("a.txt")
        assert sub.delete_file("b.txt")
        assert not sub.exists("b.txt")

    def test_resolve_path_is_absolute_under_subdir(self, tmp_path):
        _, sub, _ = _mk(tmp_path)
        resolved = sub.resolve_path("a.txt")
        assert resolved.endswith("/wt/a.txt")

    def test_leading_slash_input_normalized(self, tmp_path):
        _, sub, _ = _mk(tmp_path)
        sub.write_file("/a.txt", "x")
        assert sub.read_file("a.txt") == "x"

    def test_delegates_unknown_attrs_to_parent(self, tmp_path):
        parent, sub, _ = _mk(tmp_path)
        # supports_shell is defined on the parent, not overridden here.
        assert sub.supports_shell == parent.supports_shell
        # is_connected / connect etc. delegate too.
        assert sub.is_connected() == parent.is_connected()


class _FakeShellBackend:
    """Minimal shell-capable backend that records calls, for shell tests."""

    root = "/ws"
    supports_shell = True

    def __init__(self):
        self.runs = []  # (command, tab_name, working_dir)
        self.sends = []  # (text, tab_name, working_dir)
        self.tabs = []  # tab names ensured/opened
        self.closed = []
        self.cancelled = []  # tab names cancelled

    def shell_run(self, command, timeout=None, tab_name="default", working_dir=None):
        self.runs.append((command, tab_name, working_dir))
        return f"ran {command} in {tab_name}"

    def shell_ensure_tab(self, name):
        self.tabs.append(name)

    def shell_send(self, name, text, enter=True, working_dir=None, allow_busy=False):
        self.sends.append((text, name, working_dir))
        return f"sent {text} to {name}"

    def shell_close_tab(self, name):
        self.closed.append(name)
        return f"closed {name}"

    def shell_cancel(self, name="default"):
        self.cancelled.append(name)
        return f"cancelled {name}"

    def shell_list_tabs(self):
        return [{"name": n} for n in self.tabs]


class TestSubdirBackendShell:
    def test_working_dir_defaults_to_subdir(self):
        fake = _FakeShellBackend()
        sub = SubdirBackend(fake, "wt")
        sub.shell_run("ls")
        _, _, wd = fake.runs[-1]
        assert wd == "wt"

    def test_working_dir_is_rerooted(self):
        fake = _FakeShellBackend()
        sub = SubdirBackend(fake, "wt")
        sub.shell_run("ls", working_dir="sub")
        assert fake.runs[-1][2] == "wt/sub"

    def test_async_working_dir_is_rerooted(self):
        fake = _FakeShellBackend()
        sub = SubdirBackend(fake, "wt")
        sub.shell_send("default", "npm run dev", working_dir="sub")
        assert fake.sends[-1] == ("npm run dev", "default", "wt/sub")

    def test_tab_names_namespaced(self):
        fake = _FakeShellBackend()
        sub = SubdirBackend(fake, "wt", shell_tab_prefix="r0__")
        sub.shell_run("ls")  # default tab
        sub.shell_ensure_tab("build")
        assert fake.runs[-1][1] == "r0__default"
        assert "r0__build" in fake.tabs

    def test_cancel_is_namespaced(self):
        """shell_cancel must route through the tab prefix, not __getattr__
        passthrough (which would C-c the parent's un-prefixed tab)."""
        fake = _FakeShellBackend()
        sub = SubdirBackend(fake, "wt", shell_tab_prefix="r0__")
        sub.shell_cancel("default")
        assert fake.cancelled == ["r0__default"]

    def test_list_tabs_filters_and_strips(self):
        fake = _FakeShellBackend()
        fake.tabs = [
            "r0__default",
            "r0__build",
            "agent_default",
        ]  # sibling/parent tab present
        sub = SubdirBackend(fake, "wt", shell_tab_prefix="r0__")
        names = [t["name"] for t in sub.shell_list_tabs()]
        assert names == ["default", "build"]  # only this reader's, prefix stripped

    def test_close_reader_tabs_only_closes_own(self):
        fake = _FakeShellBackend()
        fake.tabs = ["r0__default", "r1__default", "agent_main"]
        sub = SubdirBackend(fake, "wt", shell_tab_prefix="r0__")
        sub.close_reader_tabs()
        assert fake.closed == ["r0__default"]  # never siblings or the shared session

    def test_no_prefix_is_passthrough(self):
        fake = _FakeShellBackend()
        sub = SubdirBackend(fake, "wt")  # no prefix
        sub.shell_run("ls", tab_name="default")
        assert fake.runs[-1][1] == "default"
