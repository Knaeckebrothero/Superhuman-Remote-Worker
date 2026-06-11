"""Tests for the rclone-subprocess ObjectStore (production virtual-tier transport).

rclone is invoked as a subprocess against a single configured remote. These
tests mock subprocess.run entirely (no rclone binary, no network) and pin the
two things that matter: the exact command + credential-env we hand rclone, and
how we parse its output (lsjson --stat for head, lsjson -R for list) including
the file-vs-prefix distinction and missing-object signalling. Mirrors the
paramiko-mock approach in test_workspace_backends.py.
"""

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

project_root = Path(__file__).parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from src.core.backends.object_store import (  # noqa: E402
    InMemoryObjectStore,
    ObjectStoreError,
)
from src.core.backends.rclone import (  # noqa: E402
    RcloneObjectStore,
    object_store_from_spec,
)


def _cp(returncode=0, stdout=b"", stderr=b"") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


@pytest.fixture
def store() -> RcloneObjectStore:
    return RcloneObjectStore(
        remote_type="s3",
        config={
            "provider": "Minio",
            "access_key_id": "AKIA",
            "secret_access_key": "shh",
            "endpoint": "http://minio.minio.svc:9000",
        },
        root="my-bucket",
    )


@pytest.fixture
def run_mock():
    with patch("src.core.backends.rclone.subprocess.run") as m:
        yield m


class TestConstruction:
    def test_requires_type(self):
        with pytest.raises(ValueError, match="remote_type"):
            RcloneObjectStore(remote_type="")

    def test_env_overlay_has_type_and_config(self, store):
        env = store._env_overlay
        assert env["RCLONE_CONFIG_SRW_TYPE"] == "s3"
        assert env["RCLONE_CONFIG_SRW_ACCESS_KEY_ID"] == "AKIA"
        assert env["RCLONE_CONFIG_SRW_SECRET_ACCESS_KEY"] == "shh"
        assert env["RCLONE_CONFIG_SRW_ENDPOINT"] == "http://minio.minio.svc:9000"

    def test_remote_path_maps_key(self, store):
        assert store._remote_path("jobs/1/a.txt") == "srw:my-bucket/jobs/1/a.txt"

    def test_remote_path_without_root(self):
        s = RcloneObjectStore(remote_type="s3", root="")
        assert s._remote_path("k") == "srw:k"


class TestGet:
    def test_get_returns_stdout_bytes(self, store, run_mock):
        run_mock.return_value = _cp(stdout=b"file bytes")
        assert store.get("jobs/1/a.txt") == b"file bytes"
        argv = run_mock.call_args.args[0]
        assert argv == ["rclone", "cat", "srw:my-bucket/jobs/1/a.txt"]

    def test_get_passes_credential_env(self, store, run_mock):
        run_mock.return_value = _cp(stdout=b"x")
        store.get("k")
        env = run_mock.call_args.kwargs["env"]
        assert env["RCLONE_CONFIG_SRW_TYPE"] == "s3"
        assert env["RCLONE_CONFIG_SRW_ACCESS_KEY_ID"] == "AKIA"

    def test_get_missing_raises_file_not_found(self, store, run_mock):
        run_mock.return_value = _cp(returncode=1, stderr=b"object not found")
        with pytest.raises(FileNotFoundError):
            store.get("ghost")

    def test_get_other_error_raises_object_store_error(self, store, run_mock):
        run_mock.return_value = _cp(returncode=1, stderr=b"AccessDenied")
        with pytest.raises(ObjectStoreError):
            store.get("k")


class TestPut:
    def test_put_pipes_data_to_rcat(self, store, run_mock):
        run_mock.return_value = _cp()
        store.put("jobs/1/a.txt", b"payload")
        argv = run_mock.call_args.args[0]
        assert argv == ["rclone", "rcat", "srw:my-bucket/jobs/1/a.txt"]
        assert run_mock.call_args.kwargs["input"] == b"payload"

    def test_put_rejects_non_bytes(self, store):
        with pytest.raises(TypeError):
            store.put("k", "not bytes")  # type: ignore[arg-type]

    def test_put_failure_raises(self, store, run_mock):
        run_mock.return_value = _cp(returncode=1, stderr=b"quota exceeded")
        with pytest.raises(ObjectStoreError):
            store.put("k", b"x")


class TestHead:
    def test_head_file_returns_size(self, store, run_mock):
        run_mock.return_value = _cp(
            stdout=b'{"Path":"a.txt","Name":"a.txt","Size":42,"IsDir":false}'
        )
        assert store.head("jobs/1/a.txt") == 42
        argv = run_mock.call_args.args[0]
        assert argv == ["rclone", "lsjson", "--stat", "srw:my-bucket/jobs/1/a.txt"]

    def test_head_directory_returns_none(self, store, run_mock):
        run_mock.return_value = _cp(stdout=b'{"Path":"d","IsDir":true,"Size":-1}')
        assert store.head("d") is None

    def test_head_missing_returns_none(self, store, run_mock):
        run_mock.return_value = _cp(returncode=1, stderr=b"directory not found")
        assert store.head("ghost") is None

    def test_head_null_returns_none(self, store, run_mock):
        run_mock.return_value = _cp(stdout=b"null")
        assert store.head("ghost") is None

    def test_head_bad_json_returns_none(self, store, run_mock):
        run_mock.return_value = _cp(stdout=b"not json")
        assert store.head("k") is None


class TestList:
    def test_list_parses_and_rebuilds_keys(self, store, run_mock):
        run_mock.return_value = _cp(
            stdout=(
                b'[{"Path":"a.txt","Size":3,"IsDir":false},'
                b'{"Path":"sub/b.txt","Size":5,"IsDir":false}]'
            )
        )
        result = store.list("jobs/1/")
        assert [(o.key, o.size) for o in result] == [
            ("jobs/1/a.txt", 3),
            ("jobs/1/sub/b.txt", 5),
        ]
        argv = run_mock.call_args.args[0]
        assert argv == [
            "rclone",
            "lsjson",
            "--recursive",
            "--files-only",
            "--no-modtime",
            "srw:my-bucket/jobs/1/",
        ]

    def test_list_filters_directories(self, store, run_mock):
        run_mock.return_value = _cp(
            stdout=(
                b'[{"Path":"d","IsDir":true,"Size":-1},'
                b'{"Path":"f.txt","IsDir":false,"Size":1}]'
            )
        )
        result = store.list("p/")
        assert [o.key for o in result] == ["p/f.txt"]

    def test_list_missing_prefix_returns_empty(self, store, run_mock):
        run_mock.return_value = _cp(returncode=1, stderr=b"directory not found")
        assert store.list("ghost/") == []

    def test_list_other_error_raises(self, store, run_mock):
        run_mock.return_value = _cp(returncode=1, stderr=b"AccessDenied")
        with pytest.raises(ObjectStoreError):
            store.list("p/")

    def test_list_sorted(self, store, run_mock):
        run_mock.return_value = _cp(
            stdout=(
                b'[{"Path":"z","Size":0,"IsDir":false},'
                b'{"Path":"a","Size":0,"IsDir":false}]'
            )
        )
        assert [o.key for o in store.list("")] == ["a", "z"]


class TestDelete:
    def test_delete_success_returns_true(self, store, run_mock):
        run_mock.return_value = _cp()
        assert store.delete("jobs/1/a.txt") is True
        argv = run_mock.call_args.args[0]
        assert argv == ["rclone", "deletefile", "srw:my-bucket/jobs/1/a.txt"]

    def test_delete_missing_returns_false(self, store, run_mock):
        run_mock.return_value = _cp(returncode=1, stderr=b"object not found")
        assert store.delete("ghost") is False

    def test_delete_other_error_raises(self, store, run_mock):
        run_mock.return_value = _cp(returncode=1, stderr=b"AccessDenied")
        with pytest.raises(ObjectStoreError):
            store.delete("k")


class TestCopy:
    def test_copy_uses_copyto(self, store, run_mock):
        run_mock.return_value = _cp()
        store.copy("a.txt", "b.txt")
        argv = run_mock.call_args.args[0]
        assert argv == [
            "rclone",
            "copyto",
            "srw:my-bucket/a.txt",
            "srw:my-bucket/b.txt",
        ]

    def test_copy_missing_source_raises_file_not_found(self, store, run_mock):
        run_mock.return_value = _cp(returncode=1, stderr=b"source object not found")
        with pytest.raises(FileNotFoundError):
            store.copy("ghost", "dst")


class TestRunErrors:
    def test_binary_missing_raises_object_store_error(self, store, run_mock):
        run_mock.side_effect = FileNotFoundError()
        with pytest.raises(ObjectStoreError, match="not found on PATH"):
            store.get("k")

    def test_timeout_raises_object_store_error(self, store, run_mock):
        run_mock.side_effect = subprocess.TimeoutExpired(cmd="rclone", timeout=1)
        with pytest.raises(ObjectStoreError, match="timed out"):
            store.get("k")


class TestConnect:
    def test_connect_ok_when_binary_present(self, store):
        with patch(
            "src.core.backends.rclone.shutil.which", return_value="/usr/bin/rclone"
        ):
            store.connect()  # must not raise

    def test_connect_raises_when_binary_missing(self, store):
        with patch("src.core.backends.rclone.shutil.which", return_value=None):
            with pytest.raises(ObjectStoreError, match="not found on PATH"):
                store.connect()


class TestObjectStoreFromSpec:
    def test_memory_type_returns_in_memory_store(self):
        s = object_store_from_spec({"type": "memory"})
        assert isinstance(s, InMemoryObjectStore)

    def test_nested_rclone_spec(self):
        s = object_store_from_spec(
            {
                "name": "workspace",
                "prefix": "jobs/1/",
                "rclone_spec": {
                    "type": "s3",
                    "config": {"access_key_id": "K"},
                    "root": "bucket-x",
                },
            }
        )
        assert isinstance(s, RcloneObjectStore)
        assert s._type == "s3"
        assert s._root == "bucket-x"
        assert s._env_overlay["RCLONE_CONFIG_SRW_ACCESS_KEY_ID"] == "K"

    def test_bare_spec(self):
        s = object_store_from_spec({"type": "webdav", "config": {"url": "http://x"}})
        assert isinstance(s, RcloneObjectStore)
        assert s._type == "webdav"

    def test_root_from_outer_bucket_field(self):
        s = object_store_from_spec(
            {"rclone_spec": {"type": "s3"}, "bucket": "outer-bucket"}
        )
        assert s._root == "outer-bucket"

    def test_missing_type_raises(self):
        with pytest.raises(ValueError, match="type"):
            object_store_from_spec({"config": {}})
