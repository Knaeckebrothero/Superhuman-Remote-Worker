"""Thread workspace upload destination resolution and object-store transport.

Covers ``services/thread_uploads.py`` across all four workspace tiers. The
``virtual`` tier is the interesting one: it has no workspace pod by design, so
before this existed every attachment 409'd with a transient-sounding message
that could never come true (see
``docs/issues/session_uploads_never_implemented_for_lite_workspace_tiers.md``).

The object-store path runs over ``InMemoryObjectStore`` — the same double the
virtual backend's own contract tests use — so nothing here needs rclone or SSH.
"""

from __future__ import annotations

import asyncio
import io
import stat
import zipfile

import pytest

from services.thread_uploads import (
    MAX_FILE_SIZE,
    MAX_FILES_PER_REQUEST,
    MAX_ZIP_ENTRIES,
    MAX_ZIP_ENTRY_UNCOMPRESSED_BYTES,
    MAX_ZIP_TOTAL_UNCOMPRESSED_BYTES,
    ThreadUploadError,
    ZipExtractionRefused,
    _expand_payloads_for_extraction,
    _plan_zip_extraction,
    _read_zip_entry_capped,
    _safe_zip_member_path,
    _sftp_write_files,
    _SshTarget,
    _VirtualTarget,
    _virtual_write_files,
    resolve_thread_upload_destination,
    upload_files_to_thread_workspace,
)
from services.workspace_binding import virtual_thread_backing_id
from src.core.backends.object_store import InMemoryObjectStore

THREAD_ID = "e6e6d412-04ba-4c82-9188-114f72bb0835"
PREFIX = f"threads/{THREAD_ID}/"
UPLOADS = f"{PREFIX}uploads/"

SPEC = {
    "type": "s3",
    "root": "srw-workspaces",
    "config": {"endpoint": "http://objects.test:9000"},
}


def _make_zip(entries: dict) -> bytes:
    """Build zip bytes from ``{name: data}`` (or ``{name: ZipInfo}`` keys
    when a test needs to set attributes like ``external_attr``)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _thread(backend: str | None = None, **metadata) -> dict:
    """A thread row with the given tier selected in its stored config_override."""
    meta: dict = dict(metadata)
    if backend is not None:
        meta.setdefault("config_override", {})["workspace"] = {"backend": backend}
    return {"id": THREAD_ID, "metadata": meta}


def _bound_virtual_thread(spec: dict = SPEC) -> dict:
    """A ``virtual`` thread bound to ``spec``, as thread-create leaves it."""
    return _thread(
        backend="virtual",
        _workspace_binding={
            "kind": "virtual",
            "backing_id": virtual_thread_backing_id(THREAD_ID, spec),
            "generation": "1c2e86d3-bf97-4ce4-9028-9fc9f91044ac",
        },
    )


@pytest.fixture
def virtual_env(monkeypatch):
    """A deployment with an object store configured and rclone present."""
    from services import workspace_binding

    monkeypatch.setattr(
        workspace_binding, "virtual_workspace_rclone_spec", lambda: SPEC
    )
    monkeypatch.setattr(
        workspace_binding.shutil, "which", lambda name: f"/usr/bin/{name}"
    )


# =============================================================================
# Destination resolution
# =============================================================================


class TestSshDestinations:
    def test_ready_container_resolves_to_ssh(self, monkeypatch):
        from services import thread_uploads

        monkeypatch.setattr(thread_uploads, "resolve_ssh_key_path", lambda: "/key")
        thread = _thread(
            backend="sandbox",
            workspace_container={"status": "ready", "pod_ip": "10.42.0.9"},
        )

        target = resolve_thread_upload_destination(thread)

        assert isinstance(target, _SshTarget)
        assert (target.host, target.port) == ("10.42.0.9", 22)

    def test_ready_vm_wins_over_container(self, monkeypatch):
        from services import thread_uploads

        monkeypatch.setattr(thread_uploads, "resolve_ssh_key_path", lambda: "/key")
        thread = _thread(
            backend="vm",
            vm={"status": "ready", "ssh_host": "100.64.0.3", "ssh_port": 2222},
            workspace_container={"status": "ready", "pod_ip": "10.42.0.9"},
        )

        target = resolve_thread_upload_destination(thread)

        assert (target.host, target.port) == ("100.64.0.3", 2222)

    def test_unready_pod_tier_keeps_the_transient_message(self):
        """Still the right advice — this branch only fires where a pod is coming."""
        thread = _thread(
            backend="sandbox", workspace_container={"status": "provisioning"}
        )

        with pytest.raises(ThreadUploadError) as err:
            resolve_thread_upload_destination(thread)

        assert err.value.status_code == 409
        assert "try again in a moment" in err.value.detail

    def test_missing_ssh_key_is_a_deployment_problem(self, monkeypatch):
        from services import thread_uploads

        monkeypatch.setattr(thread_uploads, "resolve_ssh_key_path", lambda: "")
        thread = _thread(
            backend="sandbox",
            workspace_container={"status": "ready", "pod_ip": "10.42.0.9"},
        )

        with pytest.raises(ThreadUploadError) as err:
            resolve_thread_upload_destination(thread)

        assert err.value.status_code == 503


class TestLiteDestinations:
    def test_virtual_resolves_to_the_threads_object_prefix(self, virtual_env):
        target = resolve_thread_upload_destination(_bound_virtual_thread())

        assert isinstance(target, _VirtualTarget)
        assert target.prefix == PREFIX
        assert target.spec == SPEC

    def test_virtual_ignores_gitea_only_workspace_container(self, virtual_env):
        """`_setup_gitea` writes workspace_container for *every* tier.

        It carries repo coordinates and no status, so a presence check would
        misread the tier. Resolution must key on the selected backend.
        """
        thread = _bound_virtual_thread()
        thread["metadata"]["workspace_container"] = {
            "repo_name": "thread-e6e6d412",
            "git_remote_url": "http://gitea/srw/thread-e6e6d412.git",
        }

        assert isinstance(resolve_thread_upload_destination(thread), _VirtualTarget)

    def test_none_tier_is_refused_permanently(self):
        with pytest.raises(ThreadUploadError) as err:
            resolve_thread_upload_destination(_thread(backend="none"))

        assert err.value.status_code == 409
        assert "try again" not in err.value.detail.lower()
        assert "no workspace" in err.value.detail.lower()

    def test_unconfigured_object_store_is_503(self, monkeypatch):
        from services import workspace_binding

        monkeypatch.setattr(
            workspace_binding, "virtual_workspace_rclone_spec", lambda: None
        )

        with pytest.raises(ThreadUploadError) as err:
            resolve_thread_upload_destination(_bound_virtual_thread())

        assert err.value.status_code == 503

    def test_memory_spec_is_not_a_real_backing(self, monkeypatch):
        """Process-local storage is invisible to the other replica."""
        from services import workspace_binding

        monkeypatch.setattr(
            workspace_binding,
            "virtual_workspace_rclone_spec",
            lambda: {"type": "memory", "root": "", "config": {}},
        )

        with pytest.raises(ThreadUploadError) as err:
            resolve_thread_upload_destination(_bound_virtual_thread())

        assert err.value.status_code == 503

    def test_missing_rclone_is_503(self, monkeypatch):
        from services import workspace_binding

        monkeypatch.setattr(
            workspace_binding, "virtual_workspace_rclone_spec", lambda: SPEC
        )
        monkeypatch.setattr(workspace_binding.shutil, "which", lambda name: None)

        with pytest.raises(ThreadUploadError) as err:
            resolve_thread_upload_destination(_bound_virtual_thread())

        assert err.value.status_code == 503

    def test_unbound_thread_is_refused_not_silently_rebound(self, virtual_env):
        with pytest.raises(ThreadUploadError) as err:
            resolve_thread_upload_destination(_thread(backend="virtual"))

        assert err.value.status_code == 409

    def test_stale_binding_is_refused(self, virtual_env):
        thread = _bound_virtual_thread()
        thread["metadata"]["_workspace_binding"]["backing_id"] = "rclone:stale"

        with pytest.raises(ThreadUploadError) as err:
            resolve_thread_upload_destination(thread)

        assert err.value.status_code == 409

    def test_json_string_metadata_is_tolerated(self, virtual_env):
        import json

        thread = _bound_virtual_thread()
        thread["metadata"] = json.dumps(thread["metadata"])

        assert isinstance(resolve_thread_upload_destination(thread), _VirtualTarget)


# =============================================================================
# Zip extraction — transport-agnostic core
# =============================================================================


class TestSafeZipMemberPath:
    """Deliberate traversal rejection, unlike the worker's ``_extract_zip``
    (``src/agent.py:4067``), whose dotfile skip only *incidentally* also
    blocks ".." because ".." happens to start with "."."""

    def test_ordinary_nested_entry_resolves_under_stem(self):
        assert _safe_zip_member_path("bundle", "sub/file.txt") == "bundle/sub/file.txt"

    def test_harmless_internal_dotdot_is_normalized_not_rejected(self):
        """A "../" that stays within the entry's own subtree isn't a
        traversal attempt — only escaping *stem* is."""
        assert _safe_zip_member_path("bundle", "sub/../file.txt") == "bundle/file.txt"

    def test_rejects_traversal_escaping_stem(self):
        assert _safe_zip_member_path("bundle", "../../etc/passwd") is None

    def test_rejects_traversal_that_exactly_cancels_stem(self):
        assert _safe_zip_member_path("bundle", "sub/../..") is None

    def test_rejects_absolute_path(self):
        assert _safe_zip_member_path("bundle", "/etc/passwd") is None

    def test_rejects_backslash_separator(self):
        assert _safe_zip_member_path("bundle", "sub\\evil.dll") is None

    def test_rejects_windows_drive_letter(self):
        assert _safe_zip_member_path("bundle", "C:/Windows/evil.dll") is None

    def test_rejects_nul_byte(self):
        assert _safe_zip_member_path("bundle", "evil\x00.txt") is None

    def test_rejects_empty_name(self):
        assert _safe_zip_member_path("bundle", "") is None


class TestReadZipEntryCapped:
    """The chunked-read cap is what actually defends against a zip bomb —
    a declared ``file_size``/CRC can lie about what a stream decompresses
    to, so bounding *actual* bytes read is the real backstop."""

    def test_reads_under_cap_normally(self):
        fh = io.BytesIO(b"hello world")
        assert _read_zip_entry_capped(fh, cap=1000, entry_name="x") == b"hello world"

    def test_aborts_when_actual_bytes_exceed_cap_regardless_of_any_claim(self):
        # Nothing here claims a size at all — this proves the cap is
        # enforced against bytes actually produced, not metadata.
        fh = io.BytesIO(b"x" * 1000)

        with pytest.raises(ZipExtractionRefused):
            _read_zip_entry_capped(fh, cap=100, entry_name="huge")


class TestPlanZipExtraction:
    def test_extracts_entries_under_stem_preserving_structure(self):
        data = _make_zip({"a.txt": b"hello", "sub/b.txt": b"world"})

        plan = _plan_zip_extraction(data, stem="bundle")

        assert dict(plan) == {"bundle/a.txt": b"hello", "bundle/sub/b.txt": b"world"}

    def test_skips_directory_entries_dotfiles_and_macosx(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("real.txt", b"hello")
            zf.writestr("dir/", b"")
            zf.writestr(".hidden", b"secret")
            zf.writestr("__MACOSX/._real.txt", b"junk")
            zf.writestr("sub/.git/config", b"nope")

        plan = _plan_zip_extraction(buf.getvalue(), stem="bundle")

        assert dict(plan) == {"bundle/real.txt": b"hello"}

    def test_skips_symlink_entries(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("real.txt", b"hello")
            link = zipfile.ZipInfo("link")
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            zf.writestr(link, "/etc/passwd")

        plan = _plan_zip_extraction(buf.getvalue(), stem="bundle")

        assert dict(plan) == {"bundle/real.txt": b"hello"}

    def test_rejects_traversal_entry_refusing_the_whole_archive(self):
        """Zip-Slip: one unsafe entry refuses the batch rather than
        extracting everything else and silently dropping just that one —
        "rather than half-extracted" (task brief)."""
        data = _make_zip({"good.txt": b"fine", "../../etc/passwd": b"pwned"})

        with pytest.raises(ZipExtractionRefused):
            _plan_zip_extraction(data, stem="bundle")

    def test_rejects_corrupt_non_zip_bytes(self):
        with pytest.raises(ZipExtractionRefused):
            _plan_zip_extraction(b"not actually a zip file", stem="bundle")

    def test_rejects_empty_bytes(self):
        with pytest.raises(ZipExtractionRefused):
            _plan_zip_extraction(b"", stem="bundle")

    def test_rejects_too_many_entries(self, monkeypatch):
        from services import thread_uploads

        monkeypatch.setattr(thread_uploads, "MAX_ZIP_ENTRIES", 3)
        data = _make_zip({f"f{i}.txt": b"x" for i in range(4)})

        with pytest.raises(ZipExtractionRefused):
            _plan_zip_extraction(data, stem="bundle")

    def test_rejects_entry_over_the_per_entry_cap(self, monkeypatch):
        from services import thread_uploads

        monkeypatch.setattr(thread_uploads, "MAX_ZIP_ENTRY_UNCOMPRESSED_BYTES", 10)
        data = _make_zip({"big.bin": b"x" * 11})

        with pytest.raises(ZipExtractionRefused):
            _plan_zip_extraction(data, stem="bundle")

    def test_rejects_when_total_uncompressed_exceeds_the_total_cap(self, monkeypatch):
        from services import thread_uploads

        monkeypatch.setattr(thread_uploads, "MAX_ZIP_ENTRY_UNCOMPRESSED_BYTES", 100)
        monkeypatch.setattr(thread_uploads, "MAX_ZIP_TOTAL_UNCOMPRESSED_BYTES", 150)
        data = _make_zip({"a.bin": b"x" * 100, "b.bin": b"x" * 100})

        with pytest.raises(ZipExtractionRefused):
            _plan_zip_extraction(data, stem="bundle")

    def test_real_world_caps_relate_sensibly_to_max_file_size(self):
        """Documents the chosen relationship so a future edit to
        MAX_FILE_SIZE doesn't silently detune these — see task-1 report."""
        assert MAX_ZIP_ENTRY_UNCOMPRESSED_BYTES == MAX_FILE_SIZE
        assert MAX_ZIP_TOTAL_UNCOMPRESSED_BYTES == 3 * MAX_FILE_SIZE
        assert MAX_ZIP_ENTRIES == 2_000


class TestExpandPayloadsForExtraction:
    def test_regular_payload_passes_through_sanitized_and_claimed(self):
        taken: set[str] = set()

        expanded = _expand_payloads_for_extraction(
            [("notes.md", b"hi", "text/markdown")], taken
        )

        assert expanded == [("notes.md", b"hi", "text/markdown")]
        assert taken == {"notes.md"}

    def test_regular_payload_collision_resolves_like_before(self):
        taken = {"notes.md"}

        expanded = _expand_payloads_for_extraction(
            [("notes.md", b"hi", "text/markdown")], taken
        )

        assert expanded == [("notes_1.md", b"hi", "text/markdown")]

    def test_zip_payload_expands_into_members_under_its_stem(self):
        data = _make_zip({"a.txt": b"hello", "sub/b.txt": b"world"})
        taken: set[str] = set()

        expanded = _expand_payloads_for_extraction(
            [("bundle.zip", data, "application/zip")], taken
        )

        names = {name for name, _, _ in expanded}
        assert names == {"bundle/a.txt", "bundle/sub/b.txt"}
        assert "bundle" in taken

    def test_zip_member_mime_type_is_guessed_from_its_own_extension(self):
        data = _make_zip({"photo.jpg": b"fake-jpeg-bytes"})

        expanded = _expand_payloads_for_extraction(
            [("bundle.zip", data, "application/zip")], set()
        )

        [(name, _, mime_type)] = expanded
        assert name == "bundle/photo.jpg"
        assert mime_type == "image/jpeg"

    def test_two_zips_with_the_same_stem_get_distinct_directories(self):
        data = _make_zip({"a.txt": b"1"})
        taken: set[str] = set()

        expanded = _expand_payloads_for_extraction(
            [
                ("bundle.zip", data, "application/zip"),
                ("bundle.zip", data, "application/zip"),
            ],
            taken,
        )

        names = {name for name, _, _ in expanded}
        assert names == {"bundle/a.txt", "bundle_1/a.txt"}

    def test_zip_stem_colliding_with_an_existing_name_is_renamed(self):
        taken = {"bundle"}
        data = _make_zip({"a.txt": b"1"})

        expanded = _expand_payloads_for_extraction(
            [("bundle.zip", data, "application/zip")], taken
        )

        assert expanded == [("bundle_1/a.txt", b"1", "text/plain")]

    def test_corrupt_zip_falls_back_to_storing_the_original_bytes_verbatim(self):
        garbage = b"not actually a zip file"

        expanded = _expand_payloads_for_extraction(
            [("bundle.zip", garbage, "application/zip")], set()
        )

        assert expanded == [("bundle.zip", garbage, "application/zip")]

    def test_traversal_zip_falls_back_to_storing_the_original_bytes_verbatim(self):
        """Half-extracted is not an option: the whole archive is refused
        and the user keeps exactly what they uploaded."""
        data = _make_zip({"good.txt": b"fine", "../../etc/passwd": b"pwned"})

        expanded = _expand_payloads_for_extraction(
            [("evil.zip", data, "application/zip")], set()
        )

        assert expanded == [("evil.zip", data, "application/zip")]

    def test_zip_with_only_filtered_entries_falls_back_to_verbatim(self):
        """A zip containing only __MACOSX/dotfile junk extracts to nothing
        useful — keep the original rather than silently storing an empty
        directory."""
        data = _make_zip({".hidden": b"x", "__MACOSX/._x": b"y"})

        expanded = _expand_payloads_for_extraction(
            [("junk.zip", data, "application/zip")], set()
        )

        assert expanded == [("junk.zip", data, "application/zip")]

    def test_fallback_reuses_the_claimed_stem_slot_avoiding_a_second_collision(self):
        """The stem is claimed once whether extraction succeeds or falls
        back — so the fallback name can never collide with something else
        claimed in between."""
        taken = {"bundle"}
        garbage = b"not actually a zip file"

        expanded = _expand_payloads_for_extraction(
            [("bundle.zip", garbage, "application/zip")], taken
        )

        assert expanded == [("bundle_1.zip", garbage, "application/zip")]


# =============================================================================
# Object-store transport
# =============================================================================


class TestVirtualWrite:
    @pytest.fixture
    def store(self) -> InMemoryObjectStore:
        return InMemoryObjectStore()

    @pytest.fixture
    def target(self) -> _VirtualTarget:
        return _VirtualTarget(spec=SPEC, prefix=PREFIX)

    def test_writes_under_the_threads_uploads_prefix(self, target, store):
        result = _virtual_write_files(
            target, [("report.pdf", b"body", "application/pdf")], store=store
        )

        assert store.get(f"{UPLOADS}report.pdf") == b"body"
        assert result[0].path == "uploads/report.pdf"
        assert result[0].name == "report.pdf"
        assert result[0].size == 4
        assert result[0].mime_type == "application/pdf"

    def test_no_directory_marker_is_written(self, target, store):
        """is_dir() is emergent from key prefixes; a marker would just be litter."""
        _virtual_write_files(target, [("a.txt", b"x", "text/plain")], store=store)

        assert [info.key for info in store.list(PREFIX)] == [f"{UPLOADS}a.txt"]

    def test_collides_with_existing_object(self, target, store):
        store.put(f"{UPLOADS}notes.md", b"old")

        result = _virtual_write_files(
            target, [("notes.md", b"new", "text/markdown")], store=store
        )

        assert result[0].name == "notes_1.md"
        assert store.get(f"{UPLOADS}notes_1.md") == b"new"
        assert store.get(f"{UPLOADS}notes.md") == b"old"

    def test_duplicate_names_within_one_batch_do_not_clobber(self, target, store):
        """Session e6e6d412 attached 'Themen Proposal.pdf' twice in one send."""
        result = _virtual_write_files(
            target,
            [
                ("Themen Proposal.pdf", b"first", "application/pdf"),
                ("Themen Proposal.pdf", b"second", "application/pdf"),
            ],
            store=store,
        )

        names = [r.name for r in result]
        assert names[0] == "Themen Proposal.pdf"
        assert names[1] == "Themen Proposal_1.pdf"
        assert store.get(f"{UPLOADS}Themen Proposal.pdf") == b"first"
        assert store.get(f"{UPLOADS}Themen Proposal_1.pdf") == b"second"

    def test_nested_keys_do_not_shadow_a_flat_name(self, target, store):
        """Only direct children of uploads/ can collide with an upload name."""
        store.put(f"{UPLOADS}sub/report.pdf", b"nested")

        result = _virtual_write_files(
            target, [("report.pdf", b"body", "application/pdf")], store=store
        )

        assert result[0].name == "report.pdf"

    def test_traversal_cannot_escape_the_prefix(self, target, store):
        result = _virtual_write_files(
            target, [("../../etc/passwd", b"x", "text/plain")], store=store
        )

        assert result[0].name == "passwd"
        assert store.get(f"{UPLOADS}passwd") == b"x"
        assert all(info.key.startswith(UPLOADS) for info in store.list(""))

    def test_blank_mime_falls_back_to_octet_stream(self, target, store):
        result = _virtual_write_files(target, [("x.bin", b"x", "")], store=store)

        assert result[0].mime_type == "application/octet-stream"

    def test_zip_payload_is_extracted_into_a_stem_directory(self, target, store):
        data = _make_zip({"a.txt": b"hello", "sub/b.txt": b"world"})

        result = _virtual_write_files(
            target, [("bundle.zip", data, "application/zip")], store=store
        )

        names = {r.name for r in result}
        assert names == {"bundle/a.txt", "bundle/sub/b.txt"}
        assert store.get(f"{UPLOADS}bundle/a.txt") == b"hello"
        assert store.get(f"{UPLOADS}bundle/sub/b.txt") == b"world"
        paths = {r.path for r in result}
        assert paths == {"uploads/bundle/a.txt", "uploads/bundle/sub/b.txt"}

    def test_traversal_entry_inside_zip_falls_back_to_verbatim_storage(
        self, target, store
    ):
        """No partial extraction: the whole archive is stored as the
        original upload rather than half-expanded."""
        data = _make_zip({"good.txt": b"fine", "../../etc/passwd": b"pwned"})

        result = _virtual_write_files(
            target, [("evil.zip", data, "application/zip")], store=store
        )

        assert result[0].name == "evil.zip"
        assert store.get(f"{UPLOADS}evil.zip") == data
        assert all(info.key.startswith(UPLOADS) for info in store.list(""))

    def test_existing_extracted_directory_blocks_a_new_zip_stem_collision(
        self, target, store
    ):
        """A previous zip already extracted to uploads/bundle/... — a new
        zip that would also stem to "bundle" must not land in the same
        directory and mix with unrelated content."""
        store.put(f"{UPLOADS}bundle/old.txt", b"from the first upload")
        data = _make_zip({"new.txt": b"from the second upload"})

        result = _virtual_write_files(
            target, [("bundle.zip", data, "application/zip")], store=store
        )

        assert result[0].name == "bundle_1/new.txt"
        assert store.get(f"{UPLOADS}bundle/old.txt") == b"from the first upload"
        assert store.get(f"{UPLOADS}bundle_1/new.txt") == b"from the second upload"

    def test_zip_and_regular_file_in_one_batch_do_not_collide(self, target, store):
        """Files and zip stems share one namespace: the plain "bundle"
        claims that name first (payload order), so the zip's stem resolves
        to "bundle_1" rather than clobbering it."""
        data = _make_zip({"a.txt": b"hello"})

        result = _virtual_write_files(
            target,
            [
                ("bundle", b"a plain file, not a zip", "text/plain"),
                ("bundle.zip", data, "application/zip"),
            ],
            store=store,
        )

        names = {r.name for r in result}
        assert names == {"bundle", "bundle_1/a.txt"}
        assert store.get(f"{UPLOADS}bundle") == b"a plain file, not a zip"
        assert store.get(f"{UPLOADS}bundle_1/a.txt") == b"hello"


# =============================================================================
# SFTP transport
#
# _sftp_write_files had no unit coverage at all before this — it needs a
# fake SFTPClient rather than the InMemoryObjectStore the virtual transport
# tests use. The fake tracks directories (via mkdir) and written files
# in-memory so assertions don't need a real SSH connection.
# =============================================================================


class _FakeSftpFile:
    def __init__(self, sink: dict, path: str):
        self._sink = sink
        self._path = path
        self._buf = b""

    def write(self, data: bytes) -> None:
        self._buf += data

    def __enter__(self) -> "_FakeSftpFile":
        return self

    def __exit__(self, *exc_info) -> bool:
        self._sink[self._path] = self._buf
        return False


class _FakeSftp:
    """Minimal paramiko SFTPClient stand-in for ``_sftp_write_files``."""

    def __init__(self):
        self.dirs: set[str] = {""}
        self.files: dict[str, bytes] = {}

    def stat(self, path: str):
        if path in self.dirs or path in self.files:
            return object()
        raise FileNotFoundError(path)

    def mkdir(self, path: str) -> None:
        self.dirs.add(path)

    def listdir(self, path: str) -> list[str]:
        prefix = path.rstrip("/") + "/"
        children: set[str] = set()
        for existing in (*self.dirs, *self.files):
            if existing.startswith(prefix) and existing != path:
                remainder = existing[len(prefix) :]
                if remainder:
                    children.add(remainder.split("/", 1)[0])
        return sorted(children)

    def open(self, path: str, mode: str) -> _FakeSftpFile:
        return _FakeSftpFile(self.files, path)

    def close(self) -> None:
        pass


@pytest.fixture
def sftp_env(monkeypatch):
    """Patch paramiko.SSHClient so _sftp_write_files runs against a fake
    in-memory SFTP filesystem instead of a real connection."""
    import paramiko

    from unittest.mock import MagicMock

    fake_sftp = _FakeSftp()
    mock_ssh = MagicMock()
    mock_ssh.open_sftp.return_value = fake_sftp
    monkeypatch.setattr(paramiko, "SSHClient", MagicMock(return_value=mock_ssh))
    return fake_sftp


def _ssh_target() -> _SshTarget:
    return _SshTarget(
        host="10.0.0.5",
        port=22,
        username="agent-host",
        key_path="/key",
        workspace_path="/home/agent-host/workspace",
    )


class TestSftpWrite:
    UPLOADS_DIR = "/home/agent-host/workspace/uploads"

    def test_regular_file_upload_still_works(self, sftp_env):
        """Regression coverage for the taken-set refactor: collision
        resolution must behave exactly as the old live-probe loop did."""
        result = _sftp_write_files(
            _ssh_target(), [("notes.md", b"hello", "text/markdown")]
        )

        assert result[0].name == "notes.md"
        assert result[0].path == "uploads/notes.md"
        assert sftp_env.files[f"{self.UPLOADS_DIR}/notes.md"] == b"hello"

    def test_collision_resolves_against_an_existing_remote_file(self, sftp_env):
        sftp_env.files[f"{self.UPLOADS_DIR}/notes.md"] = b"old"

        result = _sftp_write_files(
            _ssh_target(), [("notes.md", b"new", "text/markdown")]
        )

        assert result[0].name == "notes_1.md"
        assert sftp_env.files[f"{self.UPLOADS_DIR}/notes_1.md"] == b"new"
        assert sftp_env.files[f"{self.UPLOADS_DIR}/notes.md"] == b"old"

    def test_zip_payload_is_extracted_into_nested_remote_paths(self, sftp_env):
        data = _make_zip({"a.txt": b"hello", "sub/b.txt": b"world"})

        result = _sftp_write_files(
            _ssh_target(), [("bundle.zip", data, "application/zip")]
        )

        names = {r.name for r in result}
        assert names == {"bundle/a.txt", "bundle/sub/b.txt"}
        assert sftp_env.files[f"{self.UPLOADS_DIR}/bundle/a.txt"] == b"hello"
        assert sftp_env.files[f"{self.UPLOADS_DIR}/bundle/sub/b.txt"] == b"world"
        # Nested parent directories were created before the write.
        assert f"{self.UPLOADS_DIR}/bundle" in sftp_env.dirs
        assert f"{self.UPLOADS_DIR}/bundle/sub" in sftp_env.dirs

    def test_traversal_zip_falls_back_to_verbatim_storage(self, sftp_env):
        data = _make_zip({"good.txt": b"fine", "../../etc/passwd": b"pwned"})

        result = _sftp_write_files(
            _ssh_target(), [("evil.zip", data, "application/zip")]
        )

        assert result[0].name == "evil.zip"
        assert sftp_env.files[f"{self.UPLOADS_DIR}/evil.zip"] == data

    def test_corrupt_zip_falls_back_to_verbatim_storage(self, sftp_env):
        garbage = b"not actually a zip file"

        result = _sftp_write_files(
            _ssh_target(), [("broken.zip", garbage, "application/zip")]
        )

        assert result[0].name == "broken.zip"
        assert sftp_env.files[f"{self.UPLOADS_DIR}/broken.zip"] == garbage


# =============================================================================
# Service-level limits and routing
# =============================================================================


class TestLimitsAndRouting:
    @pytest.mark.asyncio
    async def test_empty_payload_is_rejected(self, virtual_env):
        with pytest.raises(ThreadUploadError) as err:
            await upload_files_to_thread_workspace(_bound_virtual_thread(), [])

        assert err.value.status_code == 400

    @pytest.mark.asyncio
    async def test_too_many_files(self, virtual_env):
        payloads = [
            (f"f{i}.txt", b"x", "text/plain") for i in range(MAX_FILES_PER_REQUEST + 1)
        ]

        with pytest.raises(ThreadUploadError) as err:
            await upload_files_to_thread_workspace(_bound_virtual_thread(), payloads)

        assert err.value.status_code == 400

    @pytest.mark.asyncio
    async def test_oversize_file(self, virtual_env):
        payloads = [("big.bin", b"x" * (MAX_FILE_SIZE + 1), "application/octet-stream")]

        with pytest.raises(ThreadUploadError) as err:
            await upload_files_to_thread_workspace(_bound_virtual_thread(), payloads)

        assert err.value.status_code == 413

    @pytest.mark.asyncio
    async def test_virtual_thread_routes_to_the_object_store_writer(
        self, virtual_env, monkeypatch
    ):
        from services import thread_uploads

        seen: dict = {}

        def fake_write(target, payloads):
            seen["target"] = target
            seen["payloads"] = payloads
            return []

        monkeypatch.setattr(thread_uploads, "_virtual_write_files", fake_write)

        await upload_files_to_thread_workspace(
            _bound_virtual_thread(), [("a.txt", b"x", "text/plain")]
        )

        assert isinstance(seen["target"], _VirtualTarget)
        assert seen["target"].prefix == PREFIX

    @pytest.mark.asyncio
    async def test_pre_resolved_destination_is_honoured(self, monkeypatch):
        """The endpoint resolves first, then hands the destination back in."""
        from services import thread_uploads

        called: dict = {}

        def fake_write(target, payloads):
            called["target"] = target
            return []

        monkeypatch.setattr(thread_uploads, "_virtual_write_files", fake_write)

        # A thread whose own metadata would refuse — proving the passed-in
        # destination is used rather than re-resolved.
        await upload_files_to_thread_workspace(
            _thread(backend="virtual"),
            [("a.txt", b"x", "text/plain")],
            destination=_VirtualTarget(spec=SPEC, prefix=PREFIX),
        )

        assert called["target"].prefix == PREFIX

    @pytest.mark.asyncio
    async def test_saturated_upload_capacity_fails_fast(self, monkeypatch):
        from services import thread_uploads

        saturated = asyncio.Semaphore(1)
        await saturated.acquire()
        monkeypatch.setattr(thread_uploads, "_VIRTUAL_UPLOAD_SEMAPHORE", saturated)
        monkeypatch.setattr(thread_uploads, "VIRTUAL_UPLOAD_QUEUE_TIMEOUT", 0.01)

        try:
            with pytest.raises(ThreadUploadError) as err:
                await upload_files_to_thread_workspace(
                    _thread(backend="virtual"),
                    [("a.txt", b"x", "text/plain")],
                    destination=_VirtualTarget(spec=SPEC, prefix=PREFIX),
                )
        finally:
            saturated.release()

        assert err.value.status_code == 503
