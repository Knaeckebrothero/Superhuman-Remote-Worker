"""derive_manifest — synthetic-tar cases for every classification rule (spec §5)."""

import io
import tarfile

import pytest

from orchestrator.services.cloud_staging.manifest import derive_manifest


def _build_tar(tmp_path, members):
    """members: list of (name, kind, data, pax) tuples; kind in file|chr|dir."""
    p = tmp_path / "upper.tar"
    with tarfile.open(p, "w", format=tarfile.PAX_FORMAT) as tf:
        for name, kind, data, pax in members:
            ti = tarfile.TarInfo(name=name)
            if pax:
                ti.pax_headers = pax
            if kind == "chr":
                ti.type = tarfile.CHRTYPE
                ti.devmajor = 0
                ti.devminor = 0
                tf.addfile(ti)
            elif kind == "dir":
                ti.type = tarfile.DIRTYPE
                tf.addfile(ti)
            else:
                ti.size = len(data)
                tf.addfile(ti, io.BytesIO(data))
    return str(p)


def test_added_vs_modified_by_baseline_membership(tmp_path):
    tar = _build_tar(
        tmp_path,
        [
            ("upper/new.txt", "file", b"hello", None),
            ("upper/old.txt", "file", b"world", None),
        ],
    )
    m = derive_manifest(tar, baseline={"old.txt": "e1"}, epoch=3, staged_at="t")
    st = {e["path"]: e["status"] for e in m["entries"]}
    assert st == {"new.txt": "added", "old.txt": "modified"}
    assert m["counts"] == {"added": 1, "modified": 1, "deleted": 0}
    assert m["epoch"] == 3
    assert m["skipped"] == []


def test_char_whiteout_expands_to_baseline_files(tmp_path):
    # whiteout of a DIRECTORY deletes every baseline file under it
    tar = _build_tar(tmp_path, [("upper/docs", "chr", b"", None)])
    m = derive_manifest(
        tar,
        baseline={"docs/a.txt": "e", "docs/b/c.txt": "e", "keep.txt": "e"},
        epoch=1,
        staged_at="t",
    )
    assert {e["path"] for e in m["entries"]} == {"docs/a.txt", "docs/b/c.txt"}
    assert all(e["status"] == "deleted" for e in m["entries"])


def test_whiteout_of_never_in_lower_path_is_noop(tmp_path):
    tar = _build_tar(tmp_path, [("upper/ghost.txt", "chr", b"", None)])
    m = derive_manifest(tar, baseline={"real.txt": "e"}, epoch=1, staged_at="t")
    assert m["entries"] == []


def test_opaque_dir_deletes_unshadowed_baseline_files_only(tmp_path):
    tar = _build_tar(
        tmp_path,
        [
            ("upper/proj", "dir", b"", {"SCHILY.xattr.user.fuseoverlayfs.opaque": "y"}),
            ("upper/proj/kept.txt", "file", b"v2", None),
        ],
    )
    m = derive_manifest(
        tar,
        baseline={"proj/kept.txt": "e", "proj/gone.txt": "e"},
        epoch=1,
        staged_at="t",
    )
    st = {e["path"]: e["status"] for e in m["entries"]}
    assert st == {"proj/kept.txt": "modified", "proj/gone.txt": "deleted"}


def test_opaque_dir_never_in_lower_is_pure_add(tmp_path):
    # fuse-overlayfs marks every merged-created dir opaque (whiteout.py phase-0 note)
    tar = _build_tar(
        tmp_path,
        [
            (
                "upper/newdir",
                "dir",
                b"",
                {"SCHILY.xattr.user.fuseoverlayfs.opaque": "y"},
            ),
            ("upper/newdir/f.txt", "file", b"x", None),
        ],
    )
    m = derive_manifest(tar, baseline={}, epoch=1, staged_at="t")
    assert [(e["path"], e["status"]) for e in m["entries"]] == [
        ("newdir/f.txt", "added")
    ]


def test_wh_name_marker_and_sentinel_and_bookkeeping(tmp_path):
    tar = _build_tar(
        tmp_path,
        [
            ("upper/a/.wh.dead.txt", "file", b"", None),  # xattr-format whiteout
            ("upper/b/.wh..wh..opq", "file", b"", None),  # opaque sentinel for b/
            ("upper/.wh..opq", "chr", b"", None),  # engine bookkeeping char dev -> skip
        ],
    )
    m = derive_manifest(
        tar, baseline={"a/dead.txt": "e", "b/old.txt": "e"}, epoch=1, staged_at="t"
    )
    st = {e["path"]: e["status"] for e in m["entries"]}
    assert st == {"a/dead.txt": "deleted", "b/old.txt": "deleted"}


def test_bare_wh_prefix_raises(tmp_path):
    tar = _build_tar(tmp_path, [("upper/x/.wh.", "file", b"", None)])
    with pytest.raises(ValueError):
        derive_manifest(tar, baseline={}, epoch=1, staged_at="t")


def test_binary_sniff_and_size(tmp_path):
    tar = _build_tar(
        tmp_path,
        [
            ("upper/img.png", "file", b"\x89PNG\x00\x1a", None),
            ("upper/note.md", "file", b"plain text", None),
        ],
    )
    m = derive_manifest(tar, baseline={}, epoch=1, staged_at="t")
    by = {e["path"]: e for e in m["entries"]}
    assert by["img.png"]["binary"] is True and by["img.png"]["size"] == 6
    assert by["note.md"]["binary"] is False


def test_non_regular_members_surface_as_skipped(tmp_path):
    p = tmp_path / "upper.tar"
    with tarfile.open(p, "w", format=tarfile.PAX_FORMAT) as tf:
        ti = tarfile.TarInfo(name="upper/link.txt")
        ti.type = tarfile.SYMTYPE
        ti.linkname = "target.txt"
        tf.addfile(ti)
    m = derive_manifest(str(p), baseline={}, epoch=1, staged_at="t")
    assert m["entries"] == []
    assert m["skipped"] == [{"path": "link.txt", "kind": "symlink"}]


def test_select_protected_mount_picks_first_nextcloud_with_handle():
    from orchestrator.services.cloud_staging import select_protected_mount

    rows = [
        {"backend_id": "opencloud", "cloud_handle": "h0"},
        {"backend_id": "nextcloud", "cloud_handle": None},
        {
            "mount_kind": "project_default",
            "backend_id": "nextcloud",
            "cloud_handle": "personal-home",
        },
        {
            "mount_kind": "project",
            "backend_id": "nextcloud",
            "cloud_handle": "h1",
            "mountpoint": "Proj",
        },
        {"mount_kind": "project", "backend_id": "nextcloud", "cloud_handle": "h2"},
    ]
    assert select_protected_mount(rows)["cloud_handle"] == "h1"
    assert select_protected_mount([]) is None
