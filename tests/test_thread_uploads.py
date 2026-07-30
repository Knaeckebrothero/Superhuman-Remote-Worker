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

import pytest

from services.thread_uploads import (
    MAX_FILE_SIZE,
    MAX_FILES_PER_REQUEST,
    ThreadUploadError,
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
