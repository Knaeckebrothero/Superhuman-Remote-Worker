"""Tests for the cloud_sync base class — algorithm only, no transport."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

import pytest

from src.services.cloud_sync.base import WorkspaceSyncBase, _should_ignore


class FakeSync(WorkspaceSyncBase):
    """Records calls so tests can assert on the algorithm's choices."""

    def __init__(self, *args, remote_listing: Optional[list[dict]] = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.uploads: list[tuple[str, str]] = []
        self.mkdirs: list[str] = []
        self.downloads: list[tuple[str, str]] = []
        self.ready_calls = 0
        self._listing = remote_listing or []

    async def _ensure_ready(self) -> None:
        self.ready_calls += 1

    async def _ensure_remote_dir(self, rel_dir: str) -> None:
        self.mkdirs.append(rel_dir)

    async def _upload_file(self, rel_path: str, local_path: str) -> None:
        self.uploads.append((rel_path, local_path))

    async def _list_remote_files(self) -> list[dict]:
        return list(self._listing)

    async def _download_file(self, rel_path: str, local_path: str) -> None:
        self.downloads.append((rel_path, local_path))
        Path(local_path).write_text("remote-content")


@pytest.mark.parametrize(
    "path,expected",
    [
        (".git/HEAD", True),
        ("src/.git/foo", True),
        ("repos/x", True),
        ("todos.yaml", True),
        ("some/todos.yaml", True),
        ("archive/phase_1.yaml", True),
        ("workspace.md", True),
        ("plan.md", True),
        ("output/result.txt", False),
        ("notes.md", False),
        ("deep/nested/file.py", False),
    ],
)
def test_should_ignore(path, expected):
    assert _should_ignore(path) is expected


@pytest.mark.asyncio
async def test_push_local_dedup_by_mtime(tmp_path: Path):
    (tmp_path / "a.txt").write_text("hi")
    (tmp_path / "b.txt").write_text("ho")
    sync = FakeSync(tmp_path)

    first = await sync.push()
    assert set(first) == {"a.txt", "b.txt"}
    assert {u[0] for u in sync.uploads} == {"a.txt", "b.txt"}

    # Second push without changes — nothing uploaded
    sync.uploads.clear()
    second = await sync.push()
    assert second == []
    assert sync.uploads == []


@pytest.mark.asyncio
async def test_push_skips_ignored_files(tmp_path: Path):
    (tmp_path / "keep.txt").write_text("keep")
    (tmp_path / "todos.yaml").write_text("ignored")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ignored")

    sync = FakeSync(tmp_path)
    pushed = await sync.push()
    assert pushed == ["keep.txt"]


@pytest.mark.asyncio
async def test_pull_etag_dedup(tmp_path: Path):
    listing = [
        {"path": "a.txt", "etag": "v1", "isdir": False},
        {"path": "dir/", "etag": "", "isdir": True},
        {"path": "todos.yaml", "etag": "v1", "isdir": False},
    ]
    sync = FakeSync(tmp_path, remote_listing=listing)

    first = await sync.pull()
    assert first == ["a.txt"]  # dir skipped, todos.yaml skipped

    # Same etag → no re-download
    sync.downloads.clear()
    second = await sync.pull()
    assert second == []


@pytest.mark.asyncio
async def test_pull_refetch_on_etag_change(tmp_path: Path):
    sync = FakeSync(
        tmp_path,
        remote_listing=[
            {"path": "a.txt", "etag": "v1", "isdir": False},
        ],
    )
    await sync.pull()
    sync._listing = [{"path": "a.txt", "etag": "v2", "isdir": False}]
    sync.downloads.clear()
    pulled = await sync.pull()
    assert pulled == ["a.txt"]


@pytest.mark.asyncio
async def test_full_sync_returns_tuple(tmp_path: Path):
    (tmp_path / "a.txt").write_text("hi")
    sync = FakeSync(
        tmp_path,
        remote_listing=[{"path": "b.txt", "etag": "v1", "isdir": False}],
    )
    pushed, pulled = await sync.full_sync()
    assert pushed == ["a.txt"]
    assert pulled == ["b.txt"]


@pytest.mark.asyncio
async def test_ensure_remote_dirs_caches(tmp_path: Path):
    sync = FakeSync(tmp_path)
    await sync._ensure_remote_dirs("a/b/c")
    # Should have called mkdir for each segment
    assert sync.mkdirs == ["a", "a/b", "a/b/c"]
    sync.mkdirs.clear()
    # Second call, same path — cache kicks in
    await sync._ensure_remote_dirs("a/b/c")
    assert sync.mkdirs == []


@pytest.mark.asyncio
async def test_poll_cancels_cleanly(tmp_path: Path):
    sync = FakeSync(tmp_path)
    sync._poll_interval = 1  # any positive value
    await sync.start_background_poll()
    # Give the task a chance to schedule but not complete a full sleep
    await asyncio.sleep(0.01)
    await sync.stop()
    assert sync._poll_task is None
    assert sync._running is False
