"""Tests for the S3-backed per-user IDE profile-blob store.

``IdeProfileStore`` wraps the snapshot boto3 client to stash blobs that don't
fit JSONB: the code-server ``globalStorage`` bundle (license/activation state)
and the bytes of any extension Open VSX can't provide. The store must return
False (not raise) on a missing object so the seed path degrades gracefully.
"""

import pytest

from orchestrator.services.ide_profile_store import IdeProfileStore

UID = "11111111-1111-1111-1111-111111111111"


class FakeS3:
    """Minimal in-memory stand-in for the boto3 S3 client surface we use."""

    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body):
        self.objects[(Bucket, Key)] = Body.read() if hasattr(Body, "read") else Body

    def get_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            from botocore.exceptions import ClientError

            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        body = self.objects[(Bucket, Key)]

        class _B:
            def __init__(self_inner):
                self_inner.offset = 0

            def read(self_inner, amount=-1):
                if amount is None or amount < 0:
                    amount = len(body) - self_inner.offset
                chunk = body[self_inner.offset : self_inner.offset + amount]
                self_inner.offset += len(chunk)
                return chunk

        return {"Body": _B()}

    def head_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            from botocore.exceptions import ClientError

            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
        return {}


def test_globalstorage_key_layout():
    store = IdeProfileStore(FakeS3(), "srw-snapshots")
    assert store.globalstorage_key(UID) == f"ide-profiles/{UID}/globalStorage.tar.zst"
    assert (
        store.ext_bytes_key(UID, "a.b", "1.0.0")
        == f"ide-profiles/{UID}/ext/a.b/1.0.0.tar.zst"
    )


@pytest.mark.asyncio
async def test_put_then_get_globalstorage_roundtrip(tmp_path):
    s3 = FakeS3()
    store = IdeProfileStore(s3, "srw-snapshots")
    src = tmp_path / "gs.tar.zst"
    src.write_bytes(b"BLOB")
    pointer = await store.put_globalstorage(UID, str(src))
    dst = tmp_path / "out.tar.zst"
    ok = await store.get_globalstorage(UID, str(dst), pointer)
    assert ok and dst.read_bytes() == b"BLOB"


@pytest.mark.asyncio
async def test_get_missing_returns_false(tmp_path):
    store = IdeProfileStore(FakeS3(), "srw-snapshots")
    assert await store.get_globalstorage(UID, str(tmp_path / "x")) is False


@pytest.mark.asyncio
async def test_ext_bytes_exists(tmp_path):
    s3 = FakeS3()
    store = IdeProfileStore(s3, "srw-snapshots")
    assert await store.ext_bytes_exists(UID, "a.b", "1.0.0") is False
    src = tmp_path / "e.tar.zst"
    src.write_bytes(b"E")
    pointer = await store.put_ext_bytes(UID, "a.b", "1.0.0", str(src))
    assert await store.ext_bytes_exists(UID, "a.b", "1.0.0", pointer) is True


@pytest.mark.asyncio
@pytest.mark.parametrize("stored_global", [False, True])
async def test_seed_missing_profile_blob_still_releases_workspace(stored_global):
    """A real store removes missing downloads; cleanup must tolerate that."""
    from pathlib import Path
    from unittest.mock import AsyncMock

    from orchestrator.services.ide_settings import (
        SEED_STATE_SENTINEL,
        seed_ide_profile,
    )

    s3 = FakeS3()
    store = IdeProfileStore(s3, "srw-snapshots")
    if stored_global:
        s3.objects[("srw-snapshots", store.globalstorage_key(UID))] = b"PROFILE"

    downloads = []
    original_get = store._get

    async def record_download(pointer, local_path, **kwargs):
        downloads.append(Path(local_path))
        return await original_get(pointer, local_path, **kwargs)

    store._get = record_download
    runner = AsyncMock(return_value=(0, b"", b""))
    restored = []

    async def push(host, port, local_path, **kwargs):
        restored.append(Path(local_path).read_bytes())
        return True

    ready = await seed_ide_profile(
        user_id=UID,
        ssh_host="workspace",
        ssh_port=22,
        profile_store=store,
        ext_items={"a.b": {"version": "1.0.0", "source": "bytes"}},
        _runner=runner,
        _push_fn=push,
    )

    assert ready is True
    runner.assert_awaited_once()
    assert SEED_STATE_SENTINEL in runner.await_args.args[2]
    assert restored == ([b"PROFILE"] if stored_global else [])
    assert len(downloads) == 2
    assert all(not path.exists() for path in downloads)
