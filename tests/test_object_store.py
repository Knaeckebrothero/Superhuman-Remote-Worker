"""Tests for the ObjectStore seam and its in-memory implementation.

The ObjectStore is the narrow flat key/value blob seam that
VirtualWorkspaceBackend builds file operations on (see
docs/features/no_workspace_agent_mode.md §5). These tests pin the seam's
contract: byte fidelity, missing-object signalling (FileNotFoundError vs
None), prefix listing, and idempotent delete. The backend's own contract is
exercised in test_virtual_workspace_backend.py over this same store.
"""

import sys
from pathlib import Path

import pytest

project_root = Path(__file__).parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from src.core.backends.object_store import (  # noqa: E402
    InMemoryObjectStore,
    ObjectInfo,
    ObjectStore,
)


@pytest.fixture
def store() -> InMemoryObjectStore:
    return InMemoryObjectStore()


class TestPutGet:
    def test_put_then_get_roundtrips_bytes(self, store):
        store.put("a/b.txt", b"hello")
        assert store.get("a/b.txt") == b"hello"

    def test_put_overwrites(self, store):
        store.put("k", b"one")
        store.put("k", b"two")
        assert store.get("k") == b"two"

    def test_get_missing_raises_file_not_found(self, store):
        with pytest.raises(FileNotFoundError):
            store.get("ghost")

    def test_put_rejects_non_bytes(self, store):
        with pytest.raises(TypeError):
            store.put("k", "not bytes")  # type: ignore[arg-type]

    def test_put_accepts_bytearray_and_stores_immutable_copy(self, store):
        buf = bytearray(b"abc")
        store.put("k", buf)
        buf[0] = ord("z")  # mutate after put
        assert store.get("k") == b"abc"  # stored copy is unaffected

    def test_put_empty_object(self, store):
        store.put("empty", b"")
        assert store.get("empty") == b""
        assert store.head("empty") == 0


class TestHead:
    def test_head_returns_size(self, store):
        store.put("k", b"12345")
        assert store.head("k") == 5

    def test_head_missing_returns_none(self, store):
        assert store.head("ghost") is None

    def test_head_does_not_raise_on_missing(self, store):
        # Distinguishing feature vs get(): head never raises for absence.
        assert store.head("nope") is None


class TestList:
    def test_list_by_prefix(self, store):
        store.put("jobs/1/a.txt", b"a")
        store.put("jobs/1/sub/b.txt", b"bb")
        store.put("jobs/2/c.txt", b"ccc")

        result = store.list("jobs/1/")
        keys = [o.key for o in result]
        assert keys == ["jobs/1/a.txt", "jobs/1/sub/b.txt"]

    def test_list_is_recursive(self, store):
        store.put("p/x", b"x")
        store.put("p/deep/nested/y", b"yy")
        assert len(store.list("p/")) == 2

    def test_list_empty_prefix_lists_all(self, store):
        store.put("a", b"a")
        store.put("b", b"b")
        assert len(store.list("")) == 2

    def test_list_returns_object_info_with_size(self, store):
        store.put("k", b"abcd")
        (info,) = store.list("k")
        assert isinstance(info, ObjectInfo)
        assert info.key == "k"
        assert info.size == 4

    def test_list_sorted_by_key(self, store):
        store.put("z", b"")
        store.put("a", b"")
        store.put("m", b"")
        assert [o.key for o in store.list("")] == ["a", "m", "z"]

    def test_list_no_match_returns_empty(self, store):
        store.put("a", b"a")
        assert store.list("nomatch/") == []


class TestDelete:
    def test_delete_existing_returns_true(self, store):
        store.put("k", b"v")
        assert store.delete("k") is True
        assert store.head("k") is None

    def test_delete_missing_returns_false(self, store):
        assert store.delete("ghost") is False

    def test_delete_is_idempotent(self, store):
        store.put("k", b"v")
        assert store.delete("k") is True
        assert store.delete("k") is False  # second delete: no error, False


class TestCopy:
    def test_copy_duplicates_object(self, store):
        store.put("src", b"data")
        store.copy("src", "dst")
        assert store.get("dst") == b"data"
        assert store.get("src") == b"data"  # source preserved

    def test_copy_missing_source_raises(self, store):
        with pytest.raises(FileNotFoundError):
            store.copy("ghost", "dst")


class TestLifecycle:
    def test_connect_and_close_are_noops(self, store):
        # Default lifecycle hooks must be safe to call on the in-memory store.
        store.connect()
        store.put("k", b"v")
        store.close()
        assert store.get("k") == b"v"


class TestInterface:
    def test_in_memory_store_is_object_store(self, store):
        assert isinstance(store, ObjectStore)

    def test_implements_all_abstract_methods(self, store):
        for method in ("get", "put", "head", "list", "delete"):
            assert callable(getattr(store, method))
