from __future__ import annotations
import os
from pathlib import Path

import pytest

from src.services.cloud_overlay.whiteout import enumerate_diff, DiffEntry, is_whiteout


def _mkchardev_or_skip(p: Path):
    try:
        os.mknod(p, 0o600 | 0o020000, os.makedev(0, 0))  # S_IFCHR, 0:0
    except PermissionError:
        import pytest

        pytest.skip(
            "mknod c 0,0 denied by backing FS/kernel "
            "(needs Linux >=5.8 and a non-overlayfs-backed tmpdir)"
        )


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
    (d / ".wh..wh..opq").write_text("")  # opaque sentinel
    (d / "kept.txt").write_text("x")
    got = enumerate_diff(str(tmp_path))
    assert DiffEntry("docs", "deleted") in got  # lower dir wiped
    assert DiffEntry("docs/kept.txt", "present") in got
    assert all(e.path != "docs/.wh..wh..opq" for e in got)  # sentinel hidden


def test_metadata_xattr_files_never_leak_as_paths(tmp_path):
    # fuse-overlayfs bookkeeping lives in xattrs, not extra files — it must
    # not appear as diff entries nor duplicate the real path
    f = tmp_path / "real.txt"
    f.write_text("x")
    try:
        os.setxattr(f, "user.fuseoverlayfs.origin", b"x")
        os.setxattr(f, "user.containers.override_stat", b"0:0:0755")
    except OSError:
        pytest.skip("xattrs unsupported on this filesystem")
    got = enumerate_diff(str(tmp_path))
    assert got == [DiffEntry("real.txt", "present")]


def test_bare_whiteout_marker_at_root_raises(tmp_path):
    (tmp_path / ".wh.").write_text("")  # prefix with empty remainder: malformed
    with pytest.raises(ValueError, match="malformed whiteout marker"):
        enumerate_diff(str(tmp_path))


def test_bare_whiteout_marker_nested_raises(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / ".wh.").write_text("")  # must not alias to 'sub' itself
    with pytest.raises(ValueError, match="malformed whiteout marker"):
        enumerate_diff(str(tmp_path))


def test_is_whiteout_rejects_bare_and_sentinel_names():
    assert is_whiteout(".wh.gone.txt")
    assert not is_whiteout(".wh.")  # marker with no target is not valid
    assert not is_whiteout(".wh..wh..opq")  # opaque sentinel, not a whiteout
    assert not is_whiteout("regular.txt")


def test_char_device_named_dotwh_opq_is_engine_bookkeeping_not_deletion(tmp_path):
    # Live-discovered fuse-overlayfs artifact: after `mv lowerdir newdir`, the
    # renamed dir's upperdir gets a char(0,0) node literally named `.wh..opq`
    # — a whiteout OF the opaque-sentinel name, i.e. engine bookkeeping, not
    # a deleted user file (overlay-reserved names can never be real files).
    sub = tmp_path / "newdir"
    sub.mkdir()
    _mkchardev_or_skip(sub / ".wh..opq")
    (sub / "kept.txt").write_text("x")
    got = enumerate_diff(str(tmp_path))
    assert got == [DiffEntry("newdir/kept.txt", "present")]


def test_char_device_named_dotwh_foo_is_engine_bookkeeping_not_deletion(tmp_path):
    # Generic rule, not just the `.opq` special case: ANY char(0,0) node
    # whose name starts with `.wh.` is bookkeeping and must be skipped.
    _mkchardev_or_skip(tmp_path / ".wh.foo")
    (tmp_path / "kept.txt").write_text("x")
    got = enumerate_diff(str(tmp_path))
    assert got == [DiffEntry("kept.txt", "present")]
