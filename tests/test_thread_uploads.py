"""Thread workspace upload destination resolution and object-store transport.

Covers ``services/thread_uploads.py`` across all four workspace tiers. The
``virtual`` tier is the interesting one: it has no workspace pod by design, so
before this existed every attachment 409'd with a transient-sounding message
that could never come true (see
``knowledge-base/knowledge/issues/session_uploads_never_implemented_for_lite_workspace_tiers.md``).

The object-store path runs over ``InMemoryObjectStore`` — the same double the
virtual backend's own contract tests use — so nothing here needs rclone or SSH.
"""

from __future__ import annotations

import asyncio
import io
import posixpath
import stat
import zipfile

import pytest

from services.thread_uploads import (
    MAX_FILE_SIZE,
    MAX_FILES_PER_REQUEST,
    MAX_ZIP_ENTRIES_PER_REQUEST,
    MAX_ZIP_ENTRY_UNCOMPRESSED_BYTES,
    MAX_ZIP_TOTAL_UNCOMPRESSED_BYTES_PER_REQUEST,
    ZIP_REFUSAL_NOTE_SUFFIX,
    ThreadUploadError,
    ZipExtractionRefused,
    _expand_payloads_for_extraction,
    _plan_zip_extraction,
    _read_zip_entry_capped,
    _safe_upload_relpath,
    _safe_zip_member_path,
    _sftp_delete_file,
    _sftp_write_files,
    _SshTarget,
    _virtual_delete_file,
    _VirtualTarget,
    _virtual_write_files,
    delete_file_from_thread_workspace,
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


class TestSafeUploadRelpath:
    """The DELETE route's path validator — the load-bearing part of that
    endpoint, since getting it wrong yields an arbitrary-file-deletion
    primitive.

    ``_sanitize_filename`` is deliberately NOT reused: it flattens to
    ``PurePosixPath(name).name``, which would destroy the
    ``bundle/sub/a.txt`` shape a zip-extracted member legitimately has. The
    posture here is ``_safe_zip_member_path``'s instead — reject, never
    sanitize — because a *sanitized* traversal is a silently redirected
    delete, not a fixed-up name.
    """

    def test_rejects_traversal(self):
        assert _safe_upload_relpath("../../.ssh/authorized_keys") is None
        assert _safe_upload_relpath("uploads/../../etc/passwd") is None

    def test_rejects_absolute_and_windows_and_nul(self):
        assert _safe_upload_relpath("/etc/passwd") is None
        assert _safe_upload_relpath("C:\\evil") is None
        assert _safe_upload_relpath("a\x00b") is None

    def test_rejects_empty(self):
        assert _safe_upload_relpath("") is None

    def test_rejects_backslash_anywhere_not_just_a_drive_letter(self):
        assert _safe_upload_relpath("sub\\evil.dll") is None

    def test_rejects_a_path_resolving_to_uploads_itself(self):
        """ "." and "bundle/.." both land on uploads/ — removing an
        attachment chip must never mean "delete the whole directory"."""
        assert _safe_upload_relpath(".") is None
        assert _safe_upload_relpath("./") is None
        assert _safe_upload_relpath("bundle/..") is None

    def test_rejects_escape_to_a_sibling_of_uploads(self):
        """The thread prefix is SHARED — canvas_files.py:1034 writes Canvas
        state at threads/<id>/<path>, and tool files live there too. A
        validator that only confined to the thread prefix would happily let
        this through."""
        assert _safe_upload_relpath("../canvas/doc.md") is None

    def test_allows_a_flat_upload(self):
        assert _safe_upload_relpath("report.pdf") == "report.pdf"

    def test_allows_an_extracted_zip_member(self):
        assert _safe_upload_relpath("bundle/sub/a.txt") == "bundle/sub/a.txt"

    def test_allows_a_zip_stem_directory(self):
        assert _safe_upload_relpath("bundle") == "bundle"

    def test_allows_a_dotfile_inside_uploads(self):
        """A leading dot is not a traversal — only escaping uploads/ is."""
        assert _safe_upload_relpath(".hidden") == ".hidden"

    def test_normalizes_a_harmless_internal_dotdot(self):
        """Same rule as _safe_zip_member_path: a ".." that stays inside
        uploads/ isn't an attack. The NORMALIZED form is what comes back,
        because that is the string the transports must act on."""
        assert _safe_upload_relpath("bundle/sub/../a.txt") == "bundle/a.txt"

    def test_normalizes_redundant_separators_and_dots(self):
        assert _safe_upload_relpath("bundle//sub/./a.txt") == "bundle/sub/a.txt"


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

        monkeypatch.setattr(thread_uploads, "MAX_ZIP_ENTRIES_PER_REQUEST", 3)
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
        monkeypatch.setattr(
            thread_uploads, "MAX_ZIP_TOTAL_UNCOMPRESSED_BYTES_PER_REQUEST", 150
        )
        data = _make_zip({"a.bin": b"x" * 100, "b.bin": b"x" * 100})

        with pytest.raises(ZipExtractionRefused):
            _plan_zip_extraction(data, stem="bundle")

    def test_explicit_caps_override_the_module_defaults(self):
        """_expand_payloads_for_extraction passes tighter, remaining-budget
        caps per call — confirm the parameters actually take effect rather
        than the function silently falling back to the module globals."""
        data = _make_zip({"a.txt": b"x" * 50})

        with pytest.raises(ZipExtractionRefused):
            _plan_zip_extraction(data, stem="bundle", max_entry_bytes=10)

        with pytest.raises(ZipExtractionRefused):
            _plan_zip_extraction(data, stem="bundle", max_total_bytes=10)

        with pytest.raises(ZipExtractionRefused):
            _plan_zip_extraction(
                _make_zip({"a.txt": b"1", "b.txt": b"2"}), stem="bundle", max_entries=1
            )

    def test_real_world_caps_relate_sensibly_to_max_file_size(self):
        """Documents the chosen relationship so a future edit to
        MAX_FILE_SIZE doesn't silently detune these — see task-1 report."""
        assert MAX_ZIP_ENTRY_UNCOMPRESSED_BYTES == MAX_FILE_SIZE
        assert MAX_ZIP_TOTAL_UNCOMPRESSED_BYTES_PER_REQUEST == 3 * MAX_FILE_SIZE
        assert MAX_ZIP_ENTRIES_PER_REQUEST == 100


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

        assert ("bundle.zip", garbage, "application/zip") in expanded
        # A refusal note rides alongside — see TestZipRefusalNote for content
        # assertions; here just confirm nothing else was written.
        assert len(expanded) == 2
        assert expanded[1][0] == f"bundle.zip{ZIP_REFUSAL_NOTE_SUFFIX}"

    def test_traversal_zip_falls_back_to_storing_the_original_bytes_verbatim(self):
        """Half-extracted is not an option: the whole archive is refused
        and the user keeps exactly what they uploaded."""
        data = _make_zip({"good.txt": b"fine", "../../etc/passwd": b"pwned"})

        expanded = _expand_payloads_for_extraction(
            [("evil.zip", data, "application/zip")], set()
        )

        assert ("evil.zip", data, "application/zip") in expanded
        assert len(expanded) == 2
        assert expanded[1][0] == f"evil.zip{ZIP_REFUSAL_NOTE_SUFFIX}"

    def test_zip_with_only_filtered_entries_falls_back_to_verbatim(self):
        """A zip containing only __MACOSX/dotfile junk extracts to nothing
        useful — keep the original rather than silently storing an empty
        directory. No note: nothing was actually refused, an empty listing
        already says "nothing here" on its own (see TestZipRefusalNote)."""
        data = _make_zip({".hidden": b"x", "__MACOSX/._x": b"y"})

        expanded = _expand_payloads_for_extraction(
            [("junk.zip", data, "application/zip")], set()
        )

        assert expanded == [("junk.zip", data, "application/zip")]

    def test_fallback_reuses_the_claimed_stem_slot_avoiding_a_second_collision(self):
        """The stem is claimed once whether extraction succeeds or falls
        back — so if only the bare stem ("bundle") was previously taken,
        the fallback's own exact name ("bundle.zip") is free and does not
        need shifting."""
        taken = {"bundle"}
        garbage = b"not actually a zip file"

        expanded = _expand_payloads_for_extraction(
            [("bundle.zip", garbage, "application/zip")], taken
        )

        fallback_names = [n for n, _, _ in expanded]
        assert "bundle.zip" in fallback_names
        assert "bundle_1.zip" not in fallback_names

    def test_fallback_name_itself_is_shifted_when_it_collides_too(self):
        """Unlike the case above: when the exact fallback name ("bundle.zip")
        is ALSO already taken (not just the bare stem), it must shift —
        proving the fallback goes through real collision resolution against
        its own candidate, not just against the stem's."""
        taken = {"bundle", "bundle.zip"}
        garbage = b"not actually a zip file"

        expanded = _expand_payloads_for_extraction(
            [("bundle.zip", garbage, "application/zip")], taken
        )

        assert ("bundle_1.zip", garbage, "application/zip") in expanded

    def test_pre_existing_upload_with_the_exact_fallback_name_is_not_overwritten(self):
        """CRITICAL regression (review finding 1): the fallback name used to
        be assembled by string-concatenating the (possibly already-shifted)
        stem plus the original suffix, without ever checking that exact
        combined string against `taken` — only the bare stem was ever
        claimed. A pre-existing "bundle.zip" real upload was silently
        overwritten by a later corrupt/over-cap/traversal-bearing upload of
        the same name. `taken` here models that pre-existing upload having
        already claimed "bundle.zip" (not just "bundle")."""
        taken = {"bundle.zip"}
        garbage = b"not actually a zip file"

        expanded = _expand_payloads_for_extraction(
            [("bundle.zip", garbage, "application/zip")], taken
        )

        names = [n for n, _, _ in expanded]
        # The new upload must NOT land on "bundle.zip" — that name was
        # already claimed by something this function never touched.
        assert "bundle.zip" not in names
        assert ("bundle_1.zip", garbage, "application/zip") in expanded


class TestZipRefusalNote:
    """The sidecar note _expand_payloads_for_extraction writes alongside a
    refused zip's verbatim fallback (review finding 2): a valid-but-refused
    zip still parses fine, so read_file's entry listing alone can't tell
    the agent apart from a normal, successfully extracted archive."""

    def test_note_is_written_alongside_a_refused_fallback(self):
        garbage = b"not actually a zip file"

        expanded = _expand_payloads_for_extraction(
            [("broken.zip", garbage, "application/zip")], set()
        )

        note_name = f"broken.zip{ZIP_REFUSAL_NOTE_SUFFIX}"
        note = next((data for n, data, _ in expanded if n == note_name), None)
        assert note is not None
        assert b"Extraction refused" in note
        assert b"not a valid zip file" in note

    def test_note_names_the_specific_refusal_reason(self):
        data = _make_zip({"good.txt": b"fine", "../../etc/passwd": b"pwned"})

        expanded = _expand_payloads_for_extraction(
            [("evil.zip", data, "application/zip")], set()
        )

        note = next(
            data for n, data, _ in expanded if n == f"evil.zip{ZIP_REFUSAL_NOTE_SUFFIX}"
        )
        assert b"resolves outside the destination directory" in note

    def test_note_mime_type_is_text_plain(self):
        garbage = b"not actually a zip file"

        expanded = _expand_payloads_for_extraction(
            [("broken.zip", garbage, "application/zip")], set()
        )

        [mime_type] = [
            m for n, _, m in expanded if n == f"broken.zip{ZIP_REFUSAL_NOTE_SUFFIX}"
        ]
        assert mime_type == "text/plain"

    def test_no_note_when_extraction_succeeds(self):
        data = _make_zip({"a.txt": b"hi"})

        expanded = _expand_payloads_for_extraction(
            [("bundle.zip", data, "application/zip")], set()
        )

        assert not any(n.endswith(ZIP_REFUSAL_NOTE_SUFFIX) for n, _, _ in expanded)

    def test_no_note_when_zip_extracts_to_nothing_useful(self):
        data = _make_zip({".hidden": b"x"})

        expanded = _expand_payloads_for_extraction(
            [("junk.zip", data, "application/zip")], set()
        )

        assert not any(n.endswith(ZIP_REFUSAL_NOTE_SUFFIX) for n, _, _ in expanded)


class TestPerRequestZipBudget:
    """Review finding 3+4: entry count and uncompressed bytes are capped
    per upload REQUEST (shared across every zip in the batch), not reset
    per zip — a first cut of this reset the budget for each archive, so a
    full MAX_FILES_PER_REQUEST=20 batch multiplied both the memory ceiling
    and the virtual transport's serial rclone-subprocess-per-key cost by
    20x."""

    def test_entry_budget_is_shared_across_zips_in_one_batch(self, monkeypatch):
        from services import thread_uploads

        monkeypatch.setattr(thread_uploads, "MAX_ZIP_ENTRIES_PER_REQUEST", 3)
        first = _make_zip({f"f{i}.txt": b"x" for i in range(3)})  # exactly fills it
        second = _make_zip({"g.txt": b"y"})  # nothing left for this one

        expanded = _expand_payloads_for_extraction(
            [
                ("first.zip", first, "application/zip"),
                ("second.zip", second, "application/zip"),
            ],
            set(),
        )

        names = [n for n, _, _ in expanded]
        assert sum(1 for n in names if n.startswith("first/")) == 3
        # second.zip's own per-zip cap would easily allow 1 entry — it's
        # the *shared* budget, already spent by first.zip, that refuses it.
        assert "second.zip" in names

    def test_byte_budget_is_shared_across_zips_in_one_batch(self, monkeypatch):
        from services import thread_uploads

        monkeypatch.setattr(thread_uploads, "MAX_ZIP_ENTRY_UNCOMPRESSED_BYTES", 1_000)
        monkeypatch.setattr(
            thread_uploads, "MAX_ZIP_TOTAL_UNCOMPRESSED_BYTES_PER_REQUEST", 150
        )
        first = _make_zip({"a.bin": b"x" * 100})
        second = _make_zip({"b.bin": b"x" * 100})  # 100 + 100 > 150 shared budget

        expanded = _expand_payloads_for_extraction(
            [
                ("first.zip", first, "application/zip"),
                ("second.zip", second, "application/zip"),
            ],
            set(),
        )

        assert ("first/a.bin", b"x" * 100, "application/octet-stream") in expanded
        names = [n for n, _, _ in expanded]
        assert "second.zip" in names

    def test_unspent_budget_carries_forward_to_a_later_zip(self, monkeypatch):
        """Not just a hard per-zip reset in disguise: a small first zip
        leaves room for a second one, as long as both fit the shared cap."""
        from services import thread_uploads

        monkeypatch.setattr(thread_uploads, "MAX_ZIP_ENTRIES_PER_REQUEST", 2)
        first = _make_zip({"a.txt": b"1"})
        second = _make_zip({"b.txt": b"2"})

        expanded = _expand_payloads_for_extraction(
            [
                ("first.zip", first, "application/zip"),
                ("second.zip", second, "application/zip"),
            ],
            set(),
        )

        names = {n for n, _, _ in expanded}
        assert names == {"first/a.txt", "second/b.txt"}


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
        original upload rather than half-expanded. A refusal note rides
        alongside it (review finding 2) so read_file can tell this apart
        from a normal, successfully extracted archive."""
        data = _make_zip({"good.txt": b"fine", "../../etc/passwd": b"pwned"})

        result = _virtual_write_files(
            target, [("evil.zip", data, "application/zip")], store=store
        )

        assert result[0].name == "evil.zip"
        assert store.get(f"{UPLOADS}evil.zip") == data
        assert all(info.key.startswith(UPLOADS) for info in store.list(""))
        note_name = f"evil.zip{ZIP_REFUSAL_NOTE_SUFFIX}"
        assert any(r.name == note_name for r in result)
        assert b"resolves outside" in store.get(f"{UPLOADS}{note_name}")

    def test_corrupt_reupload_does_not_overwrite_an_existing_upload_of_the_same_name(
        self, target, store
    ):
        """CRITICAL regression (review finding 1): uploads/bundle.zip
        already exists as a real, previously-uploaded file. A new, corrupt
        upload also named "bundle.zip" must not silently clobber it — the
        old bug assembled the fallback name from the (possibly-shifted)
        stem plus the suffix without ever checking that exact combined
        string against what's already there."""
        store.put(f"{UPLOADS}bundle.zip", b"original good bytes")
        garbage = b"not actually a zip file"

        result = _virtual_write_files(
            target, [("bundle.zip", garbage, "application/zip")], store=store
        )

        # The old upload survives untouched.
        assert store.get(f"{UPLOADS}bundle.zip") == b"original good bytes"
        # The new (refused) upload landed somewhere else instead.
        fallback = next(
            r for r in result if not r.name.endswith(ZIP_REFUSAL_NOTE_SUFFIX)
        )
        assert fallback.name != "bundle.zip"
        assert store.get(f"{UPLOADS}{fallback.name}") == garbage

    def test_traversal_reupload_does_not_overwrite_an_existing_upload_of_the_same_name(
        self, target, store
    ):
        """Same CRITICAL regression, via the traversal-refusal path rather
        than corrupt bytes — both raise ZipExtractionRefused through the
        same fallback code."""
        store.put(f"{UPLOADS}bundle.zip", b"original good bytes")
        data = _make_zip({"good.txt": b"fine", "../../etc/passwd": b"pwned"})

        result = _virtual_write_files(
            target, [("bundle.zip", data, "application/zip")], store=store
        )

        assert store.get(f"{UPLOADS}bundle.zip") == b"original good bytes"
        fallback = next(
            r for r in result if not r.name.endswith(ZIP_REFUSAL_NOTE_SUFFIX)
        )
        assert fallback.name != "bundle.zip"
        assert store.get(f"{UPLOADS}{fallback.name}") == data

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


class TestVirtualDelete:
    """Object-store side of the DELETE route. A flat key space has no
    directories, so removing a zip stem is a prefix listing plus one delete
    per key rather than a single recursive call."""

    @pytest.fixture
    def store(self) -> InMemoryObjectStore:
        return InMemoryObjectStore()

    @pytest.fixture
    def target(self) -> _VirtualTarget:
        return _VirtualTarget(spec=SPEC, prefix=PREFIX)

    def test_deletes_a_flat_key(self, target, store):
        store.put(f"{UPLOADS}report.pdf", b"bytes")

        assert _virtual_delete_file(target, "report.pdf", store=store) is True
        assert store.head(f"{UPLOADS}report.pdf") is None

    def test_a_missing_key_reports_false_rather_than_raising(self, target, store):
        """False is what the route turns into a 404 — an absent file is not
        an error, it's the state the caller wanted anyway."""
        assert _virtual_delete_file(target, "nope.pdf", store=store) is False

    def test_deletes_one_member_of_an_extracted_zip(self, target, store):
        store.put(f"{UPLOADS}bundle/a.txt", b"a")
        store.put(f"{UPLOADS}bundle/b.txt", b"b")

        assert _virtual_delete_file(target, "bundle/a.txt", store=store) is True
        assert store.head(f"{UPLOADS}bundle/a.txt") is None
        assert store.head(f"{UPLOADS}bundle/b.txt") is not None

    def test_deletes_every_key_under_a_zip_stem(self, target, store):
        store.put(f"{UPLOADS}bundle/a.txt", b"a")
        store.put(f"{UPLOADS}bundle/sub/b.txt", b"b")

        assert _virtual_delete_file(target, "bundle", store=store) is True
        assert store.list(f"{UPLOADS}bundle/") == []

    def test_a_stem_delete_does_not_take_prefix_siblings_with_it(self, target, store):
        """ "bundle" must not sweep up "bundle_1.txt" — the listing prefix is
        "bundle/", with the separator, not a bare startswith."""
        store.put(f"{UPLOADS}bundle/a.txt", b"a")
        store.put(f"{UPLOADS}bundle_1.txt", b"unrelated")
        store.put(f"{UPLOADS}bundlebundle.txt", b"also unrelated")

        assert _virtual_delete_file(target, "bundle", store=store) is True
        assert store.head(f"{UPLOADS}bundle_1.txt") is not None
        assert store.head(f"{UPLOADS}bundlebundle.txt") is not None

    def test_never_reaches_a_key_outside_uploads(self, target, store):
        """Canvas state is written under the SAME threads/<id>/ prefix
        (services/canvas_files.py:1034), so confinement to uploads/ —
        not merely to the thread prefix — is what protects it."""
        store.put(f"{PREFIX}canvas/doc.md", b"canvas state")
        store.put(f"{UPLOADS}report.pdf", b"bytes")

        assert _virtual_delete_file(target, "report.pdf", store=store) is True
        assert store.get(f"{PREFIX}canvas/doc.md") == b"canvas state"


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


class _FakeSftpAttrs:
    """paramiko ``SFTPAttributes`` stand-in — ``st_mode`` plus, for
    ``listdir_attr`` results, the entry's own ``filename``.

    ``st_mode`` is ``int | None`` because paramiko's is: the permissions flag
    is optional in the SFTP protocol and a server may simply not send it.
    """

    def __init__(self, st_mode: int | None, filename: str = ""):
        self.st_mode = st_mode
        self.filename = filename


class _FakeSftp:
    """Minimal paramiko SFTPClient stand-in for ``_sftp_write_files`` and
    ``_sftp_delete_file``.

    Models symlinks faithfully enough to test the delete guard: a real SFTP
    server resolves every *intermediate* component of a path before
    ``lstat`` ever sees the leaf, so ``lstat`` alone protects only the final
    component. ``_resolve_parents`` reproduces exactly that, which is what
    makes ``uploads/escape -> ~/.ssh`` plus ``escape/authorized_keys`` a
    real escape rather than a hypothetical one.
    """

    def __init__(self):
        self.dirs: set[str] = {""}
        self.files: dict[str, bytes] = {}
        self.symlinks: dict[str, str] = {}

    def _resolve_parents(self, path: str) -> str:
        """Substitute symlinked parent components, never the final one."""
        parts = [p for p in path.split("/") if p]
        cursor = "/" if path.startswith("/") else ""
        for index, part in enumerate(parts):
            cursor = posixpath.join(cursor, part) if cursor else part
            if index < len(parts) - 1 and cursor in self.symlinks:
                cursor = self.symlinks[cursor]
        return cursor

    def _mode_of(self, path: str) -> int:
        if path in self.symlinks:
            return stat.S_IFLNK | 0o777
        if path in self.files:
            return stat.S_IFREG | 0o644
        if path in self.dirs:
            return stat.S_IFDIR | 0o755
        raise FileNotFoundError(path)

    def stat(self, path: str):
        if path in self.dirs or path in self.files:
            return object()
        raise FileNotFoundError(path)

    def lstat(self, path: str) -> _FakeSftpAttrs:
        return _FakeSftpAttrs(self._mode_of(self._resolve_parents(path)))

    def mkdir(self, path: str) -> None:
        self.dirs.add(path)

    def rmdir(self, path: str) -> None:
        real = self._resolve_parents(path)
        if real not in self.dirs:
            raise FileNotFoundError(path)
        if self.listdir(real):
            raise OSError(f"Directory not empty: {path}")
        self.dirs.discard(real)

    def remove(self, path: str) -> None:
        real = self._resolve_parents(path)
        if self.symlinks.pop(real, None) is not None:
            return
        if real in self.files:
            del self.files[real]
            return
        raise FileNotFoundError(path)

    def listdir(self, path: str) -> list[str]:
        prefix = path.rstrip("/") + "/"
        children: set[str] = set()
        for existing in (*self.dirs, *self.files, *self.symlinks):
            if existing.startswith(prefix) and existing != path:
                remainder = existing[len(prefix) :]
                if remainder:
                    children.add(remainder.split("/", 1)[0])
        return sorted(children)

    def listdir_attr(self, path: str) -> list[_FakeSftpAttrs]:
        real = self._resolve_parents(path)
        return [
            _FakeSftpAttrs(self._mode_of(posixpath.join(real, name)), filename=name)
            for name in self.listdir(real)
        ]

    def open(self, path: str, mode: str) -> _FakeSftpFile:
        return _FakeSftpFile(self.files, path)

    def close(self) -> None:
        pass


class _ModelessSftp(_FakeSftp):
    """A server that omits ``SSH_FILEXFER_ATTR_PERMISSIONS``.

    Entirely legal: the permissions flag is optional in the SFTP protocol, and
    paramiko surfaces its absence as ``SFTPAttributes.st_mode is None``.
    Nothing else changes — the filesystem, and in particular the faithful
    parent-symlink resolution that makes the escape real, is inherited — so
    the *only* variable between this and ``_FakeSftp`` is the missing mode.
    """

    def lstat(self, path: str) -> _FakeSftpAttrs:
        attrs = super().lstat(path)  # still raises when the path is absent
        attrs.st_mode = None
        return attrs


def _patch_sshclient(monkeypatch, fake_sftp) -> None:
    import paramiko

    from unittest.mock import MagicMock

    mock_ssh = MagicMock()
    mock_ssh.open_sftp.return_value = fake_sftp
    monkeypatch.setattr(paramiko, "SSHClient", MagicMock(return_value=mock_ssh))


@pytest.fixture
def sftp_env(monkeypatch):
    """Patch paramiko.SSHClient so _sftp_write_files runs against a fake
    in-memory SFTP filesystem instead of a real connection."""
    fake_sftp = _FakeSftp()
    _patch_sshclient(monkeypatch, fake_sftp)
    return fake_sftp


@pytest.fixture
def modeless_sftp_env(monkeypatch):
    """``sftp_env``, but the server never reports a file's mode."""
    fake_sftp = _ModelessSftp()
    _patch_sshclient(monkeypatch, fake_sftp)
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
        note_name = f"evil.zip{ZIP_REFUSAL_NOTE_SUFFIX}"
        assert any(r.name == note_name for r in result)
        assert b"resolves outside" in sftp_env.files[f"{self.UPLOADS_DIR}/{note_name}"]

    def test_corrupt_zip_falls_back_to_verbatim_storage(self, sftp_env):
        garbage = b"not actually a zip file"

        result = _sftp_write_files(
            _ssh_target(), [("broken.zip", garbage, "application/zip")]
        )

        assert result[0].name == "broken.zip"
        assert sftp_env.files[f"{self.UPLOADS_DIR}/broken.zip"] == garbage

    def test_corrupt_reupload_does_not_overwrite_an_existing_remote_upload(
        self, sftp_env
    ):
        """CRITICAL regression (review finding 1), reproduced on the SFTP
        transport too: an existing uploads/bundle.zip must survive a later,
        corrupt re-upload of the same name."""
        sftp_env.files[f"{self.UPLOADS_DIR}/bundle.zip"] = b"original good bytes"
        garbage = b"not actually a zip file"

        result = _sftp_write_files(
            _ssh_target(), [("bundle.zip", garbage, "application/zip")]
        )

        assert (
            sftp_env.files[f"{self.UPLOADS_DIR}/bundle.zip"] == b"original good bytes"
        )
        fallback = next(
            r for r in result if not r.name.endswith(ZIP_REFUSAL_NOTE_SUFFIX)
        )
        assert fallback.name != "bundle.zip"
        assert sftp_env.files[f"{self.UPLOADS_DIR}/{fallback.name}"] == garbage


class TestSftpDelete:
    """SFTP side of the DELETE route.

    Two things this has to get right that the object-store side doesn't:
    SFTP has no ``rm -rf`` (a zip-extracted directory must be walked and
    unlinked entry by entry, then ``rmdir``'d bottom-up), and SFTP will
    happily remove ANY path the ``agent-host`` user can write — so the guard
    lives entirely on our side.
    """

    UPLOADS_DIR = "/home/agent-host/workspace/uploads"
    KEYS = "/home/agent-host/.ssh/authorized_keys"

    def _seed(self, sftp_env, *dirs: str) -> None:
        sftp_env.dirs.update({self.UPLOADS_DIR, *dirs})

    def test_deletes_a_flat_file(self, sftp_env):
        self._seed(sftp_env)
        sftp_env.files[f"{self.UPLOADS_DIR}/report.pdf"] = b"bytes"

        assert _sftp_delete_file(_ssh_target(), "report.pdf") is True
        assert f"{self.UPLOADS_DIR}/report.pdf" not in sftp_env.files

    def test_a_missing_file_reports_false_rather_than_raising(self, sftp_env):
        self._seed(sftp_env)

        assert _sftp_delete_file(_ssh_target(), "gone.pdf") is False

    def test_a_missing_uploads_dir_reports_false(self, sftp_env):
        """Nothing was ever uploaded to this workspace — a 404, not a 502."""
        assert _sftp_delete_file(_ssh_target(), "gone.pdf") is False

    def test_deletes_an_extracted_zip_directory_recursively(self, sftp_env):
        self._seed(
            sftp_env,
            f"{self.UPLOADS_DIR}/bundle",
            f"{self.UPLOADS_DIR}/bundle/sub",
        )
        sftp_env.files[f"{self.UPLOADS_DIR}/bundle/a.txt"] = b"a"
        sftp_env.files[f"{self.UPLOADS_DIR}/bundle/sub/b.txt"] = b"b"
        sftp_env.files[f"{self.UPLOADS_DIR}/keep.txt"] = b"keep"

        assert _sftp_delete_file(_ssh_target(), "bundle") is True

        assert not [k for k in sftp_env.files if "/bundle/" in k]
        assert f"{self.UPLOADS_DIR}/bundle" not in sftp_env.dirs
        assert f"{self.UPLOADS_DIR}/bundle/sub" not in sftp_env.dirs
        # The directory it lived in survives, along with its siblings.
        assert self.UPLOADS_DIR in sftp_env.dirs
        assert sftp_env.files[f"{self.UPLOADS_DIR}/keep.txt"] == b"keep"

    def test_deletes_one_member_of_an_extracted_zip(self, sftp_env):
        self._seed(sftp_env, f"{self.UPLOADS_DIR}/bundle")
        sftp_env.files[f"{self.UPLOADS_DIR}/bundle/a.txt"] = b"a"
        sftp_env.files[f"{self.UPLOADS_DIR}/bundle/b.txt"] = b"b"

        assert _sftp_delete_file(_ssh_target(), "bundle/a.txt") is True

        assert f"{self.UPLOADS_DIR}/bundle/a.txt" not in sftp_env.files
        assert sftp_env.files[f"{self.UPLOADS_DIR}/bundle/b.txt"] == b"b"
        assert f"{self.UPLOADS_DIR}/bundle" in sftp_env.dirs

    def test_a_symlink_leaf_is_unlinked_never_followed(self, sftp_env):
        """The agent can write into its own uploads/. A link planted there
        must be removed as a LINK — resolving it (stat instead of lstat)
        would delete whatever it points at, outside the tree entirely."""
        self._seed(sftp_env, "/home/agent-host/.ssh")
        sftp_env.files[self.KEYS] = b"ssh-ed25519 AAAA"
        sftp_env.symlinks[f"{self.UPLOADS_DIR}/keys"] = self.KEYS

        assert _sftp_delete_file(_ssh_target(), "keys") is True

        assert f"{self.UPLOADS_DIR}/keys" not in sftp_env.symlinks
        assert sftp_env.files[self.KEYS] == b"ssh-ed25519 AAAA"

    def test_a_symlinked_parent_component_is_refused(self, sftp_env):
        """The load-bearing case. ``lstat`` declines to follow only the FINAL
        component, so the server resolves ``escape`` on our behalf and hands
        back a perfectly ordinary regular file — one that lives in ~/.ssh. A
        leaf-only guard deletes it. Every component has to be checked."""
        self._seed(sftp_env, "/home/agent-host/.ssh")
        sftp_env.files[self.KEYS] = b"ssh-ed25519 AAAA"
        sftp_env.symlinks[f"{self.UPLOADS_DIR}/escape"] = "/home/agent-host/.ssh"

        with pytest.raises(ThreadUploadError) as err:
            _sftp_delete_file(_ssh_target(), "escape/authorized_keys")

        assert err.value.status_code == 409
        assert sftp_env.files[self.KEYS] == b"ssh-ed25519 AAAA"

    def test_a_symlinked_child_inside_a_deleted_tree_is_unlinked_not_followed(
        self, sftp_env
    ):
        """Same guard, one level down: the recursive walk must not turn a
        link inside the tree into a recursive delete of its target."""
        self._seed(sftp_env, f"{self.UPLOADS_DIR}/bundle", "/home/agent-host/.ssh")
        sftp_env.files[self.KEYS] = b"ssh-ed25519 AAAA"
        sftp_env.symlinks[f"{self.UPLOADS_DIR}/bundle/link"] = "/home/agent-host/.ssh"

        assert _sftp_delete_file(_ssh_target(), "bundle") is True

        assert f"{self.UPLOADS_DIR}/bundle" not in sftp_env.dirs
        assert sftp_env.files[self.KEYS] == b"ssh-ed25519 AAAA"
        assert "/home/agent-host/.ssh" in sftp_env.dirs

    def test_uploads_dir_itself_replaced_by_a_symlink_is_refused(self, sftp_env):
        """``rm -rf uploads && ln -s ~ uploads`` from inside the workspace
        would otherwise redirect every subsequent delete into $HOME."""
        self._seed(sftp_env, "/home/agent-host")
        sftp_env.dirs.discard(self.UPLOADS_DIR)
        sftp_env.symlinks[self.UPLOADS_DIR] = "/home/agent-host"
        sftp_env.files["/home/agent-host/.bashrc"] = b"export PATH=..."

        with pytest.raises(ThreadUploadError) as err:
            _sftp_delete_file(_ssh_target(), ".bashrc")

        assert err.value.status_code == 409
        assert sftp_env.files["/home/agent-host/.bashrc"] == b"export PATH=..."

    def test_a_server_that_reports_no_mode_cannot_walk_the_symlink(
        self, modeless_sftp_env
    ):
        """The guard must fail CLOSED on an unknown file type.

        ``SSH_FILEXFER_ATTR_PERMISSIONS`` is optional in the SFTP protocol, so
        ``st_mode`` is legitimately ``None`` against a server that omits it.
        The original ``attrs.st_mode or 0`` turned that into ``0``, and
        ``S_ISLNK(0)`` is False — so this exact scenario (the same planted
        ``uploads/escape -> ~/.ssh`` as the test above) walked straight past
        the symlink check and deleted ``authorized_keys``. Demonstrated, not
        inferred: revert ``_entry_mode`` to ``or 0`` and both assertions below
        flip together.
        """
        self._seed(modeless_sftp_env, "/home/agent-host/.ssh")
        modeless_sftp_env.files[self.KEYS] = b"ssh-ed25519 AAAA"
        modeless_sftp_env.symlinks[f"{self.UPLOADS_DIR}/escape"] = (
            "/home/agent-host/.ssh"
        )

        with pytest.raises(ThreadUploadError) as err:
            _sftp_delete_file(_ssh_target(), "escape/authorized_keys")

        assert err.value.status_code == 409
        assert modeless_sftp_env.files[self.KEYS] == b"ssh-ed25519 AAAA"

    def test_a_mode_less_ordinary_delete_is_refused_too(self, modeless_sftp_env):
        """No escape attempt at all — just a server that reports no
        permissions. Refusing costs one honest 409; guessing costs the guard."""
        self._seed(modeless_sftp_env)
        modeless_sftp_env.files[f"{self.UPLOADS_DIR}/report.pdf"] = b"bytes"

        with pytest.raises(ThreadUploadError) as err:
            _sftp_delete_file(_ssh_target(), "report.pdf")

        assert err.value.status_code == 409
        assert modeless_sftp_env.files[f"{self.UPLOADS_DIR}/report.pdf"] == b"bytes"

    def test_an_unreachable_workspace_is_a_502(self, sftp_env, monkeypatch):
        """Same taxonomy the writer uses — connect failure is 502, not a
        500 traceback."""
        import paramiko

        from unittest.mock import MagicMock

        mock_ssh = MagicMock()
        mock_ssh.connect.side_effect = OSError("no route to host")
        monkeypatch.setattr(paramiko, "SSHClient", MagicMock(return_value=mock_ssh))

        with pytest.raises(ThreadUploadError) as err:
            _sftp_delete_file(_ssh_target(), "report.pdf")

        assert err.value.status_code == 502


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


class TestDeleteRoutingAndTaxonomy:
    """``delete_file_from_thread_workspace`` — the seam the DELETE route
    sits on. Every rejection it can produce is a status the route maps
    straight through."""

    @pytest.mark.asyncio
    async def test_a_rejected_path_never_reaches_a_transport(self, monkeypatch):
        """The validator runs BEFORE destination resolution and before any
        connection: the check must never be delegated to the remote, which
        would remove whatever it was handed."""
        from services import thread_uploads

        reached: dict = {}

        def fake_delete(*args, **kwargs):
            reached["hit"] = True
            return True

        monkeypatch.setattr(thread_uploads, "_virtual_delete_file", fake_delete)
        monkeypatch.setattr(thread_uploads, "_sftp_delete_file", fake_delete)

        with pytest.raises(ThreadUploadError) as err:
            await delete_file_from_thread_workspace(
                _thread(backend="virtual"),
                "../../etc/passwd",
                destination=_VirtualTarget(spec=SPEC, prefix=PREFIX),
            )

        assert err.value.status_code == 400
        assert "hit" not in reached

    @pytest.mark.asyncio
    async def test_the_normalized_path_is_what_the_transport_receives(
        self, monkeypatch
    ):
        """Not the raw input — acting on the un-normalized string would
        undo the whole point of normalizing before the prefix check."""
        from services import thread_uploads

        seen: dict = {}

        def fake_delete(target, relpath):
            seen["relpath"] = relpath
            return True

        monkeypatch.setattr(thread_uploads, "_virtual_delete_file", fake_delete)

        await delete_file_from_thread_workspace(
            _thread(backend="virtual"),
            "bundle/sub/../a.txt",
            destination=_VirtualTarget(spec=SPEC, prefix=PREFIX),
        )

        assert seen["relpath"] == "bundle/a.txt"

    @pytest.mark.asyncio
    async def test_the_normalized_path_is_what_the_caller_gets_back(self, monkeypatch):
        """The route interpolates this straight into its 200 body. Echoing
        the caller's raw string instead reported ``bundle/sub/../a.txt`` as
        deleted while ``bundle/a.txt`` was the file that actually went."""
        from services import thread_uploads

        monkeypatch.setattr(
            thread_uploads, "_virtual_delete_file", lambda target, relpath: True
        )

        assert (
            await delete_file_from_thread_workspace(
                _thread(backend="virtual"),
                "bundle/sub/../a.txt",
                destination=_VirtualTarget(spec=SPEC, prefix=PREFIX),
            )
            == "bundle/a.txt"
        )

    @pytest.mark.asyncio
    async def test_a_virtual_thread_routes_to_the_object_store_deleter(
        self, virtual_env, monkeypatch
    ):
        from services import thread_uploads

        seen: dict = {}

        def fake_delete(target, relpath):
            seen["target"] = target
            return True

        monkeypatch.setattr(thread_uploads, "_virtual_delete_file", fake_delete)

        assert (
            await delete_file_from_thread_workspace(
                _bound_virtual_thread(), "report.pdf"
            )
            == "report.pdf"
        )
        assert isinstance(seen["target"], _VirtualTarget)
        assert seen["target"].prefix == PREFIX

    @pytest.mark.asyncio
    async def test_an_ssh_thread_routes_to_the_sftp_deleter(self, monkeypatch):
        from services import thread_uploads

        seen: dict = {}

        def fake_delete(target, relpath):
            seen["target"] = target
            return False

        monkeypatch.setattr(thread_uploads, "_sftp_delete_file", fake_delete)

        assert (
            await delete_file_from_thread_workspace(
                _thread(), "report.pdf", destination=_ssh_target()
            )
            is None
        )
        assert isinstance(seen["target"], _SshTarget)

    @pytest.mark.asyncio
    async def test_the_none_tier_is_refused_permanently(self):
        """Same 409-with-an-honest-message the upload path gives — there is
        no workspace, so there is nothing to delete from."""
        with pytest.raises(ThreadUploadError) as err:
            await delete_file_from_thread_workspace(
                _thread(backend="none"), "report.pdf"
            )

        assert err.value.status_code == 409

    @pytest.mark.asyncio
    async def test_an_unready_workspace_is_a_transient_409(self):
        with pytest.raises(ThreadUploadError) as err:
            await delete_file_from_thread_workspace(_thread(), "report.pdf")

        assert err.value.status_code == 409
        assert "not ready" in err.value.detail

    @pytest.mark.asyncio
    async def test_object_store_deletes_are_bounded_by_the_upload_semaphore(
        self, monkeypatch
    ):
        """Each delete spawns another rclone subprocess (one per key for a
        zip stem), so it shares the writer's concurrency ceiling."""
        from services import thread_uploads

        saturated = asyncio.Semaphore(1)
        await saturated.acquire()
        monkeypatch.setattr(thread_uploads, "_VIRTUAL_UPLOAD_SEMAPHORE", saturated)
        monkeypatch.setattr(thread_uploads, "VIRTUAL_UPLOAD_QUEUE_TIMEOUT", 0.01)

        try:
            with pytest.raises(ThreadUploadError) as err:
                await delete_file_from_thread_workspace(
                    _thread(backend="virtual"),
                    "report.pdf",
                    destination=_VirtualTarget(spec=SPEC, prefix=PREFIX),
                )
        finally:
            saturated.release()

        assert err.value.status_code == 503
