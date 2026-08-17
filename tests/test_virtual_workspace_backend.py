"""Contract tests for VirtualWorkspaceBackend.

VirtualWorkspaceBackend (the ``virtual`` no-workspace tier) implements the
WorkspaceBackend file contract as explicit object-store ops under a key prefix
(knowledge-base/knowledge/features/no_workspace_agent_mode.md §5). These tests run the full
contract over InMemoryObjectStore — the same contract the filesystem test
backend satisfies — plus the S3-isms the backend has to approximate
(empty-dir markers, read-modify-write append, read size guard, bounded
content search) and the prefix-isolation / boundary-escape guarantees.
"""

import sys
from pathlib import Path

import pytest

project_root = Path(__file__).parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from src.core.backends.object_store import InMemoryObjectStore  # noqa: E402
from src.core.backends.virtual import VirtualWorkspaceBackend  # noqa: E402
from src.core.workspace_backend import WorkspaceBackend  # noqa: E402

PREFIX = "jobs/job-1/"


@pytest.fixture
def store() -> InMemoryObjectStore:
    return InMemoryObjectStore()


@pytest.fixture
def backend(store) -> VirtualWorkspaceBackend:
    return VirtualWorkspaceBackend(store, prefix=PREFIX)


# =============================================================================
# Init / properties
# =============================================================================


class TestInitAndProperties:
    def test_is_a_workspace_backend(self, backend):
        assert isinstance(backend, WorkspaceBackend)

    def test_instantiable_means_all_abstract_methods_implemented(self, store):
        # If any abstract method were missing, this would raise TypeError.
        VirtualWorkspaceBackend(store)

    def test_prefix_normalized_trailing_slash(self, store):
        b = VirtualWorkspaceBackend(store, prefix="jobs/x")
        assert b.root == "jobs/x/"

    def test_prefix_strips_leading_slash(self, store):
        b = VirtualWorkspaceBackend(store, prefix="/jobs/x/")
        assert b.root == "jobs/x/"

    def test_empty_prefix(self, store):
        b = VirtualWorkspaceBackend(store, prefix="")
        assert b.root == ""

    def test_supports_shell_is_false(self, backend):
        assert backend.supports_shell is False

    def test_host_is_none(self, backend):
        assert backend.host is None


# =============================================================================
# Path resolution / boundary escapes
# =============================================================================


class TestPathResolution:
    def test_resolve_path_prepends_prefix(self, backend):
        assert (
            backend.resolve_path("knowledge-base/knowledge/a.md")
            == PREFIX + "knowledge-base/knowledge/a.md"
        )

    def test_resolve_root(self, backend):
        assert backend.resolve_path("") == PREFIX

    def test_resolve_normalizes_dots(self, backend):
        assert backend.resolve_path("a/./b/../c") == PREFIX + "a/c"

    def test_resolve_rejects_parent_traversal(self, backend):
        with pytest.raises(ValueError, match="escapes workspace boundary"):
            backend.resolve_path("../../etc/passwd")

    def test_resolve_rejects_absolute(self, backend):
        with pytest.raises(ValueError, match="escapes workspace boundary"):
            backend.resolve_path("/etc/passwd")

    def test_read_rejects_traversal(self, backend):
        with pytest.raises(ValueError, match="escapes workspace boundary"):
            backend.read_file("../secret")

    def test_write_rejects_traversal(self, backend):
        with pytest.raises(ValueError, match="escapes workspace boundary"):
            backend.write_file("../escape", "x")


# =============================================================================
# read / write / append
# =============================================================================


class TestReadWrite:
    def test_write_then_read_text(self, backend):
        backend.write_file("notes.md", "hello")
        assert backend.read_file("notes.md") == "hello"

    def test_write_then_read_binary(self, backend):
        backend.write_file("img.bin", b"\x00\x01\x02")
        assert backend.read_file("img.bin", binary=True) == b"\x00\x01\x02"

    def test_write_text_stored_as_utf8(self, backend):
        backend.write_file("u.txt", "café")
        assert backend.read_file("u.txt", binary=True) == "café".encode("utf-8")

    def test_write_overwrites(self, backend):
        backend.write_file("f", "one")
        backend.write_file("f", "two")
        assert backend.read_file("f") == "two"

    def test_write_creates_nested_path_without_mkdir(self, backend):
        backend.write_file("a/b/c/deep.txt", "x")
        assert backend.read_file("a/b/c/deep.txt") == "x"
        assert backend.is_dir("a/b/c") is True

    def test_read_missing_raises_file_not_found(self, backend):
        with pytest.raises(FileNotFoundError):
            backend.read_file("ghost.txt")

    def test_read_directory_raises_value_error(self, backend):
        backend.write_file("dir/inner.txt", "x")
        with pytest.raises(ValueError, match="Not a file"):
            backend.read_file("dir")

    def test_write_to_root_rejected(self, backend):
        with pytest.raises(ValueError, match="root"):
            backend.write_file("", "x")

    def test_write_over_directory_rejected(self, backend):
        backend.write_file("d/inner.txt", "x")
        with pytest.raises(ValueError, match="Is a directory"):
            backend.write_file("d", "clobber")

    def test_read_size_guard(self, store):
        b = VirtualWorkspaceBackend(store, prefix=PREFIX, max_read_bytes=4)
        b.write_file("big.txt", "12345")  # 5 bytes > 4
        with pytest.raises(ValueError, match="exceeds the read limit"):
            b.read_file("big.txt")


class TestAppend:
    def test_append_creates_file(self, backend):
        backend.append_file("log.txt", "line1\n")
        assert backend.read_file("log.txt") == "line1\n"

    def test_append_extends_file(self, backend):
        backend.write_file("log.txt", "a\n")
        backend.append_file("log.txt", "b\n")
        assert backend.read_file("log.txt") == "a\nb\n"


# =============================================================================
# exists / is_file / is_dir
# =============================================================================


class TestExistence:
    def test_root_is_a_directory(self, backend):
        assert backend.is_dir("") is True
        assert backend.is_file("") is False

    def test_file_exists(self, backend):
        backend.write_file("f.txt", "x")
        assert backend.exists("f.txt") is True
        assert backend.is_file("f.txt") is True
        assert backend.is_dir("f.txt") is False

    def test_directory_exists_when_it_has_children(self, backend):
        backend.write_file("d/f.txt", "x")
        assert backend.is_dir("d") is True
        assert backend.is_file("d") is False
        assert backend.exists("d") is True

    def test_nonexistent(self, backend):
        assert backend.exists("nope") is False
        assert backend.is_file("nope") is False
        assert backend.is_dir("nope") is False


# =============================================================================
# mkdir + empty-dir (.keep) handling
# =============================================================================


class TestMkdir:
    def test_mkdir_makes_empty_dir_visible(self, backend):
        backend.mkdir("empty")
        assert backend.is_dir("empty") is True

    def test_mkdir_root_is_noop(self, backend):
        backend.mkdir("")  # must not raise
        assert backend.is_dir("") is True

    def test_mkdir_nested_creates_intermediate_dirs(self, backend):
        backend.mkdir("a/b/c")
        assert backend.is_dir("a") is True
        assert backend.is_dir("a/b") is True
        assert backend.is_dir("a/b/c") is True

    def test_keep_marker_hidden_from_listing(self, backend):
        backend.mkdir("empty")
        assert backend.list_dir("empty") == []  # .keep is filtered out

    def test_empty_dir_shows_in_parent_listing(self, backend):
        backend.mkdir("sub")
        assert "sub/" in backend.list_dir("")


# =============================================================================
# list_dir
# =============================================================================


class TestListDir:
    def test_lists_immediate_children_only(self, backend):
        backend.write_file("a.txt", "x")
        backend.write_file("sub/b.txt", "y")
        backend.write_file("sub/deep/c.txt", "z")
        result = backend.list_dir("")
        assert "a.txt" in result
        assert "sub/" in result
        assert "sub/b.txt" not in result  # not immediate

    def test_directories_have_trailing_slash(self, backend):
        backend.write_file("d/inner.txt", "x")
        assert "d/" in backend.list_dir("")

    def test_relative_paths_from_root(self, backend):
        backend.write_file("sub/b.txt", "y")
        assert backend.list_dir("sub") == ["sub/b.txt"]

    def test_pattern_filter(self, backend):
        backend.write_file("a.py", "x")
        backend.write_file("b.py", "y")
        backend.write_file("c.txt", "z")
        result = backend.list_dir("", pattern="*.py")
        assert "a.py" in result and "b.py" in result
        assert "c.txt" not in result

    def test_sorted(self, backend):
        backend.write_file("z.txt", "")
        backend.write_file("a.txt", "")
        backend.write_file("m.txt", "")
        assert backend.list_dir("") == sorted(backend.list_dir(""))

    def test_file_path_returns_itself(self, backend):
        backend.write_file("solo.txt", "x")
        assert backend.list_dir("solo.txt") == ["solo.txt"]

    def test_nonexistent_returns_empty(self, backend):
        assert backend.list_dir("ghost") == []


# =============================================================================
# search_files
# =============================================================================


class TestSearch:
    def test_finds_content_match(self, backend):
        backend.write_file("a.txt", "the quick brown fox\njumps over")
        results = backend.search_files("brown")
        assert len(results) == 1
        assert results[0]["path"] == "a.txt"
        assert results[0]["line_number"] == 1
        assert "brown" in results[0]["line"]

    def test_case_insensitive_default(self, backend):
        backend.write_file("a.txt", "HELLO World")
        assert len(backend.search_files("hello")) == 1

    def test_case_sensitive(self, backend):
        backend.write_file("a.txt", "HELLO World")
        assert backend.search_files("hello", case_sensitive=True) == []
        assert len(backend.search_files("HELLO", case_sensitive=True)) == 1

    def test_skips_keep_marker(self, backend):
        backend.mkdir("d")  # writes d/.keep (empty); must not match
        assert backend.search_files("") == [] or all(
            r["path"] != "d/.keep" for r in backend.search_files("")
        )

    def test_skips_binary_extensions(self, backend):
        backend.write_file("image.png", b"needle inside binary")
        assert backend.search_files("needle") == []

    def test_respects_file_size_cap(self, store):
        b = VirtualWorkspaceBackend(store, prefix=PREFIX, search_file_bytes=4)
        b.write_file("big.txt", "needle and more text")  # > 4 bytes
        assert b.search_files("needle") == []

    def test_scoped_to_path(self, backend):
        backend.write_file("keep/a.txt", "needle here")
        backend.write_file("other/b.txt", "needle here too")
        results = backend.search_files("needle", path="keep")
        assert len(results) == 1
        assert results[0]["path"] == "keep/a.txt"


# =============================================================================
# delete_file / delete_directory
# =============================================================================


class TestDelete:
    def test_delete_existing_file(self, backend):
        backend.write_file("f.txt", "x")
        assert backend.delete_file("f.txt") is True
        assert backend.exists("f.txt") is False

    def test_delete_missing_file_returns_false(self, backend):
        assert backend.delete_file("ghost.txt") is False

    def test_delete_empty_directory_via_delete_file(self, backend):
        backend.mkdir("empty")
        assert backend.delete_file("empty") is True
        assert backend.is_dir("empty") is False

    def test_delete_nonempty_directory_via_delete_file_raises(self, backend):
        backend.write_file("d/inner.txt", "x")
        with pytest.raises(ValueError, match="non-empty"):
            backend.delete_file("d")

    def test_delete_directory_removes_subtree(self, backend):
        backend.write_file("tree/a.txt", "x")
        backend.write_file("tree/sub/b.txt", "y")
        assert backend.delete_directory("tree") is True
        assert backend.is_dir("tree") is False
        assert backend.exists("tree/a.txt") is False
        assert backend.exists("tree/sub/b.txt") is False

    def test_delete_root_raises(self, backend):
        with pytest.raises(ValueError, match="workspace root"):
            backend.delete_directory("")

    def test_delete_directory_on_file_raises(self, backend):
        backend.write_file("f.txt", "x")
        with pytest.raises(ValueError, match="Not a directory"):
            backend.delete_directory("f.txt")

    def test_delete_nonexistent_directory_returns_false(self, backend):
        assert backend.delete_directory("ghost") is False


# =============================================================================
# move / copy
# =============================================================================


class TestMoveCopy:
    def test_move_file(self, backend):
        backend.write_file("old.txt", "data")
        backend.move("old.txt", "new.txt")
        assert backend.exists("old.txt") is False
        assert backend.read_file("new.txt") == "data"

    def test_move_directory_subtree(self, backend):
        backend.write_file("src/a.txt", "1")
        backend.write_file("src/sub/b.txt", "2")
        backend.move("src", "dst")
        assert backend.exists("src/a.txt") is False
        assert backend.read_file("dst/a.txt") == "1"
        assert backend.read_file("dst/sub/b.txt") == "2"

    def test_move_missing_raises(self, backend):
        with pytest.raises(FileNotFoundError):
            backend.move("ghost", "dst")

    def test_copy_file(self, backend):
        backend.write_file("src.txt", "data")
        backend.copy("src.txt", "dst.txt")
        assert backend.read_file("src.txt") == "data"  # source preserved
        assert backend.read_file("dst.txt") == "data"

    def test_copy_directory_raises(self, backend):
        backend.write_file("d/inner.txt", "x")
        with pytest.raises(ValueError, match="Cannot copy directory"):
            backend.copy("d", "d2")

    def test_copy_missing_raises(self, backend):
        with pytest.raises(FileNotFoundError):
            backend.copy("ghost", "dst")


# =============================================================================
# stat
# =============================================================================


class TestStat:
    def test_stat_file_returns_size(self, backend):
        backend.write_file("f.txt", "12345")
        assert backend.stat("f.txt") == 5

    def test_stat_directory_sums_children(self, backend):
        backend.write_file("d/a.txt", "123")  # 3
        backend.write_file("d/b.txt", "4567")  # 4
        assert backend.stat("d") == 7

    def test_stat_nonexistent_is_zero(self, backend):
        assert backend.stat("ghost") == 0


# =============================================================================
# Lifecycle
# =============================================================================


class TestLifecycle:
    def test_not_connected_initially(self, backend):
        assert backend.is_connected() is False

    def test_connect_then_connected(self, backend):
        backend.connect()
        assert backend.is_connected() is True

    def test_disconnect(self, backend):
        backend.connect()
        backend.disconnect()
        assert backend.is_connected() is False


# =============================================================================
# Capability boundaries (no shell, no home)
# =============================================================================


class TestNoHomeNoShell:
    def test_write_home_file_unsupported(self, backend):
        with pytest.raises(NotImplementedError):
            backend.write_home_file(".ssh/key", "secret")

    def test_resolve_home_path_unsupported(self, backend):
        with pytest.raises(NotImplementedError):
            backend.resolve_home_path(".ssh/key")

    def test_shell_run_unsupported(self, backend):
        with pytest.raises(NotImplementedError):
            backend.shell_run("ls")


# =============================================================================
# Prefix isolation (durability / multi-tenant separation in one store)
# =============================================================================


class TestPrefixIsolation:
    def test_files_land_under_prefix(self, backend, store):
        backend.write_file("a.txt", "x")
        assert store.head(PREFIX + "a.txt") == 1

    def test_distinct_prefixes_isolated(self, store):
        job1 = VirtualWorkspaceBackend(store, prefix="jobs/1/")
        job2 = VirtualWorkspaceBackend(store, prefix="jobs/2/")
        job1.write_file("secret.txt", "job1 only")
        assert job2.exists("secret.txt") is False
        assert job2.list_dir("") == []
        assert job1.read_file("secret.txt") == "job1 only"


# =============================================================================
# Scoped metadata index (begin_read_cache / end_read_cache)
#
# Every store op on this backend is a process spawn in production, so session
# setup opens a scoped index to answer its dozens of existence probes from one
# listing. The index must be INVISIBLE: identical answers to the uncached
# backend, kept exact across local mutations, and gone the moment the scope
# closes.
# =============================================================================


class _CountingStore(InMemoryObjectStore):
    """InMemoryObjectStore that counts the metadata ops the index removes."""

    def __init__(self):
        super().__init__()
        self.list_calls = 0
        self.head_calls = 0

    def list(self, prefix):
        self.list_calls += 1
        return super().list(prefix)

    def head(self, key):
        self.head_calls += 1
        return super().head(key)


@pytest.fixture
def counting_store() -> _CountingStore:
    return _CountingStore()


@pytest.fixture
def counting_backend(counting_store) -> VirtualWorkspaceBackend:
    return VirtualWorkspaceBackend(counting_store, prefix=PREFIX)


class TestScopedReadCache:
    def test_inactive_by_default(self, backend):
        assert backend._index is None

    def test_probes_hit_store_once_inside_scope(self, counting_backend, counting_store):
        counting_backend.write_file("a.txt", "x")
        counting_backend.write_file("dir/b.txt", "y")
        counting_store.list_calls = 0
        counting_store.head_calls = 0

        counting_backend.begin_read_cache()
        for _ in range(5):
            assert counting_backend.is_file("a.txt") is True
            assert counting_backend.is_file("missing.txt") is False
            assert counting_backend.is_dir("dir") is True
            assert counting_backend.exists("dir/b.txt") is True
        counting_backend.end_read_cache()

        # One priming listing for 30 probes, and no per-probe head at all.
        assert counting_store.list_calls == 1
        assert counting_store.head_calls == 0

    def test_answers_match_uncached_backend(self, backend):
        backend.write_file("a.txt", "hello")
        backend.write_file("dir/b.txt", "yy")
        backend.mkdir("empty")
        probes = ["a.txt", "dir", "dir/b.txt", "empty", "missing", "dir/missing"]

        uncached = {
            p: (backend.exists(p), backend.is_file(p), backend.is_dir(p))
            for p in probes
        }
        listings = {p: backend.list_dir(p) for p in ["", "dir", "empty"]}
        walked = backend.walk("")
        stats = {p: backend.stat(p) for p in ["a.txt", "dir"]}

        backend.begin_read_cache()
        try:
            assert {
                p: (backend.exists(p), backend.is_file(p), backend.is_dir(p))
                for p in probes
            } == uncached
            assert {p: backend.list_dir(p) for p in ["", "dir", "empty"]} == listings
            assert backend.walk("") == walked
            assert {p: backend.stat(p) for p in ["a.txt", "dir"]} == stats
        finally:
            backend.end_read_cache()

    def test_local_writes_visible_inside_scope(self, backend):
        backend.begin_read_cache()
        try:
            assert backend.exists("new.txt") is False
            backend.write_file("new.txt", "content")
            assert backend.is_file("new.txt") is True
            assert backend.stat("new.txt") == 7
            assert "new.txt" in backend.list_dir("")
            backend.append_file("new.txt", "!!")
            assert backend.stat("new.txt") == 9
        finally:
            backend.end_read_cache()
        assert backend.read_file("new.txt") == "content!!"

    def test_local_deletes_visible_inside_scope(self, backend):
        backend.write_file("gone.txt", "x")
        backend.write_file("tree/deep/f.txt", "y")
        backend.begin_read_cache()
        try:
            assert backend.delete_file("gone.txt") is True
            assert backend.exists("gone.txt") is False
            assert backend.delete_directory("tree") is True
            assert backend.is_dir("tree") is False
        finally:
            backend.end_read_cache()
        assert backend.exists("gone.txt") is False
        assert backend.is_dir("tree") is False

    def test_mkdir_and_move_and_copy_tracked(self, backend):
        backend.write_file("src.txt", "abc")
        backend.begin_read_cache()
        try:
            backend.mkdir("fresh")
            assert backend.is_dir("fresh") is True
            backend.copy("src.txt", "copy.txt")
            assert backend.is_file("copy.txt") is True
            assert backend.stat("copy.txt") == 3
            backend.move("src.txt", "moved.txt")
            assert backend.is_file("src.txt") is False
            assert backend.is_file("moved.txt") is True
        finally:
            backend.end_read_cache()
        assert backend.is_dir("fresh") is True
        assert backend.read_file("moved.txt") == "abc"
        assert backend.exists("src.txt") is False

    def test_external_write_seen_after_scope_closes(self, backend, store):
        """The documented boundary: another process writing to the same prefix
        mid-scope is picked up by the first op after the scope closes."""
        backend.begin_read_cache()
        try:
            assert backend.exists("uploaded.pdf") is False
            store.put(PREFIX + "uploaded.pdf", b"externally uploaded")
            assert backend.exists("uploaded.pdf") is False  # stale, by design
        finally:
            backend.end_read_cache()
        assert backend.exists("uploaded.pdf") is True

    def test_begin_is_idempotent(self, counting_backend, counting_store):
        counting_backend.write_file("a.txt", "x")
        counting_store.list_calls = 0
        counting_backend.begin_read_cache()
        counting_backend.begin_read_cache()
        counting_backend.begin_read_cache()
        try:
            assert counting_store.list_calls == 1
        finally:
            counting_backend.end_read_cache()

    def test_end_without_begin_is_safe(self, backend):
        backend.end_read_cache()
        assert backend._index is None

    def test_prefix_isolation_holds_inside_scope(self, store):
        job1 = VirtualWorkspaceBackend(store, prefix="jobs/1/")
        job2 = VirtualWorkspaceBackend(store, prefix="jobs/2/")
        job1.write_file("secret.txt", "job1 only")
        job2.begin_read_cache()
        try:
            assert job2.exists("secret.txt") is False
            assert job2.list_dir("") == []
        finally:
            job2.end_read_cache()

    def test_mkdir_skips_rewrite_for_existing_dir_in_scope(
        self, counting_backend, counting_store
    ):
        """Scaffolding re-creates the same directories on every attach; inside
        the scope that must cost zero store writes."""
        counting_backend.mkdir("output")
        counting_backend.write_file("notes/a.txt", "x")
        counting_backend.begin_read_cache()
        try:
            before = len(counting_store._data)
            counting_backend.mkdir("output")  # marker already there
            counting_backend.mkdir("notes")  # exists via its file
            assert len(counting_store._data) == before
            assert counting_backend.is_dir("output") is True
            assert counting_backend.is_dir("notes") is True
            counting_backend.mkdir("fresh")  # genuinely new → written
            assert counting_backend.is_dir("fresh") is True
        finally:
            counting_backend.end_read_cache()
        assert counting_backend.is_dir("fresh") is True
