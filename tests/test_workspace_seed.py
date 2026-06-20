"""S3a — cross-backend workspace seeding + WorkspaceBackend.walk().

``docs/features/workspace_tier_upgrade.md`` §4.2 S3a / §4.3 W1: when a lite
(``virtual``) session/job upgrades to a real ``sandbox``, the agent copies the
object-store prefix down into the freshly provisioned backend via
``seed_workspace`` while both backends are live. This exercises:

  * ``VirtualWorkspaceBackend.walk()`` — the flat object-store override.
  * the base ``WorkspaceBackend.walk()`` — list_dir descent (via the
    filesystem test backend, which honors the root-relative list_dir contract).
  * ``seed_workspace`` — recursive copy + verify-before-return.
"""

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.core.backends.object_store import InMemoryObjectStore  # noqa: E402
from src.core.backends.seed import seed_workspace  # noqa: E402
from src.core.backends.virtual import VirtualWorkspaceBackend  # noqa: E402
from tests._fs_backend import FilesystemTestBackend  # noqa: E402


def _virtual(prefix="jobs/job-1/"):
    return VirtualWorkspaceBackend(InMemoryObjectStore(), prefix=prefix)


# ---------------------------------------------------------------------------
# walk()
# ---------------------------------------------------------------------------
class TestVirtualWalk:
    """The flat object-store override — one recursive list, markers hidden."""

    def test_walks_nested_files(self):
        b = _virtual()
        b.write_file("plan.md", "p")
        b.write_file("notes/a.md", "a")
        b.write_file("notes/sub/b.md", "b")
        assert set(b.walk()) == {"plan.md", "notes/a.md", "notes/sub/b.md"}

    def test_empty_backend_walks_to_empty_list(self):
        assert _virtual().walk() == []

    def test_skips_empty_dir_markers_and_omits_dirs(self):
        b = _virtual()
        b.write_file("a.txt", "a")
        b.mkdir("emptydir")  # drops a .keep marker, no real file
        walked = b.walk()
        assert walked == ["a.txt"]  # marker hidden, dir not itself returned

    def test_subtree_walk(self):
        b = _virtual()
        b.write_file("notes/a.md", "a")
        b.write_file("other/c.md", "c")
        assert b.walk("notes") == ["notes/a.md"]


class TestBaseWalk:
    """The default list_dir-descent walk, via the filesystem test backend."""

    def test_walks_nested_files(self, tmp_path):
        fs = FilesystemTestBackend(tmp_path)
        fs.write_file("plan.md", "p")
        fs.write_file("notes/a.md", "a")
        fs.write_file("notes/sub/b.md", "b")
        assert set(fs.walk()) == {"plan.md", "notes/a.md", "notes/sub/b.md"}

    def test_empty_backend_walks_to_empty_list(self, tmp_path):
        assert FilesystemTestBackend(tmp_path).walk() == []

    def test_agrees_with_virtual_on_same_tree(self, tmp_path):
        tree = {"plan.md": "p", "notes/a.md": "a", "notes/sub/b.md": "b"}
        v = _virtual()
        fs = FilesystemTestBackend(tmp_path)
        for rel, content in tree.items():
            v.write_file(rel, content)
            fs.write_file(rel, content)
        assert set(v.walk()) == set(fs.walk()) == set(tree)


# ---------------------------------------------------------------------------
# seed_workspace()
# ---------------------------------------------------------------------------
class TestSeedWorkspace:
    def test_copies_all_files_with_content(self, tmp_path):
        src = _virtual()
        src.write_file("plan.md", "the plan")
        src.write_file("notes/a.md", "note A")
        src.write_file("notes/sub/b.md", "note B")
        dst = FilesystemTestBackend(tmp_path)

        n = seed_workspace(src, dst)

        assert n == 3
        assert dst.read_file("plan.md") == "the plan"
        assert dst.read_file("notes/a.md") == "note A"
        assert dst.read_file("notes/sub/b.md") == "note B"

    def test_preserves_binary_content(self, tmp_path):
        src = _virtual()
        blob = b"\x00\x01\x02\xff\xfe"
        src.write_file("img.bin", blob)
        dst = FilesystemTestBackend(tmp_path)

        seed_workspace(src, dst)

        assert dst.read_file("img.bin", binary=True) == blob

    def test_empty_source_seeds_nothing(self, tmp_path):
        assert seed_workspace(_virtual(), FilesystemTestBackend(tmp_path)) == 0

    def test_creates_nested_parent_dirs(self, tmp_path):
        src = _virtual()
        src.write_file("a/b/c/deep.txt", "x")
        dst = FilesystemTestBackend(tmp_path)

        seed_workspace(src, dst)

        assert dst.is_dir("a/b/c")
        assert dst.read_file("a/b/c/deep.txt") == "x"

    def test_raises_when_destination_drops_writes(self, tmp_path, monkeypatch):
        # A dst whose writes vanish → verify-before-return must raise so the
        # caller never swaps onto a half-seeded workspace.
        src = _virtual()
        src.write_file("plan.md", "p")
        src.write_file("notes/a.md", "a")
        dst = FilesystemTestBackend(tmp_path)
        monkeypatch.setattr(dst, "write_file", lambda *a, **k: None)

        with pytest.raises(RuntimeError, match="seed incomplete"):
            seed_workspace(src, dst)
