from __future__ import annotations
import os
from pathlib import Path

from src.services.cloud_overlay.whiteout import enumerate_diff, DiffEntry


def _mkchardev_or_skip(p: Path):
    try:
        os.mknod(p, 0o600 | 0o020000, os.makedev(0, 0))  # S_IFCHR, 0:0
    except PermissionError:
        import pytest
        pytest.skip("no CAP_MKNOD in test env; char-whiteout case needs privileged runner")


def test_added_and_modified_reported_as_present(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "new.txt").write_text("hi")
    (tmp_path / "changed.md").write_text("edited")
    got = enumerate_diff(str(tmp_path))
    assert DiffEntry("changed.md", "present") in got
    assert DiffEntry("sub/new.txt", "present") in got


def test_dotwh_file_is_deletion(tmp_path):
    (tmp_path / ".wh.gone.txt").write_text("")  # unprivileged whiteout form
    got = enumerate_diff(str(tmp_path))
    assert got == [DiffEntry("gone.txt", "deleted")]


def test_char_device_is_deletion(tmp_path):
    _mkchardev_or_skip(tmp_path / "gone.bin")
    got = enumerate_diff(str(tmp_path))
    assert got == [DiffEntry("gone.bin", "deleted")]


def test_opaque_dir_sentinel_marks_dir_replaced(tmp_path):
    d = tmp_path / "docs"
    d.mkdir()
    (d / ".wh..wh..opq").write_text("")       # opaque sentinel
    (d / "kept.txt").write_text("x")
    got = enumerate_diff(str(tmp_path))
    assert DiffEntry("docs", "deleted") in got        # lower dir wiped
    assert DiffEntry("docs/kept.txt", "present") in got
    assert all(e.path != "docs/.wh..wh..opq" for e in got)  # sentinel hidden


def test_metadata_xattr_files_never_leak_as_paths(tmp_path):
    # fuse-overlayfs bookkeeping must not appear as diff entries
    (tmp_path / "real.txt").write_text("x")
    got = enumerate_diff(str(tmp_path))
    assert [e for e in got if e.path == "real.txt"]
    assert all(not e.path.startswith(".wh") for e in got)
