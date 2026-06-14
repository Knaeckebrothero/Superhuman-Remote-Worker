"""Tests for the Mode B "Open cloud folder" export flow (job_cloud_export.md).

Covers the two behaviours added when the export button moved from
``!project_id`` to "project has no cloud folder", and the export started
copying the agent's declared deliverables instead of ``output/`` wholesale:

* ``_with_cloud_review_mode`` — the pure serialization helper that turns the
  ``project_has_cloud_folder`` join column into the DTO's ``cloud_review_mode``.
* ``export_job_to_shared_folder`` — the real endpoint, driven with patched
  globals + the ``FakeMainCloudBackend``: the routing gate (project-with-cloud
  -folder → 409; default-project / loose → allowed), the deliverables copy
  (declared paths only, paths preserved, missing skipped), and the
  ``output/`` fallback for jobs without a deliverables list.

Follows the house pattern in tests/test_job_access.py: import ``main`` (conftest
puts orchestrator/ on sys.path) and patch its module globals.
"""

import json
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

import main
from tests.cloud.fake import FakeMainCloudBackend


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _make_user() -> dict:
    return {
        "id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "keycloak_sub": "kc-sub-1",
        "email": "user@example.test",
        "display_name": "User One",
        "preferred_username": "user1",
    }


def _make_job(**over) -> dict:
    job = {
        "id": "682baab8-7864-4f3b-9298-4134294f703e",
        "status": "pending_review",
        "project_id": None,
        "project_has_cloud_folder": False,
        "exported_folder_handle": None,
        "exported_at": None,
        "freeze_data": None,
        "repo_name": "job-682baab8",
    }
    job.update(over)
    return job


def _patch_endpoint(*, user, job, backend, gitea_files, repo=("job-682baab8", "main")):
    """Patch every global ``export_job_to_shared_folder`` touches.

    ``gitea_files`` maps repo-relative path -> bytes; it backs both
    ``get_file_bytes`` (deliverables path) and ``list_contents`` (output/
    fallback). Returns (ExitStack, backend, db_mock).
    """
    stack = ExitStack()
    stack.enter_context(
        patch("main.require_job_access", AsyncMock(return_value=(user, job)))
    )

    router = MagicMock()
    router.for_owner = MagicMock(return_value=backend)
    stack.enter_context(patch("main.main_cloud_router", router))

    gitea = MagicMock()
    gitea.is_initialized = True

    async def _get_file_bytes(repo_name, path, ref=None):
        return gitea_files.get(path)

    gitea.get_file_bytes = AsyncMock(side_effect=_get_file_bytes)

    async def _list_contents(repo_name, src_dir, ref=None):
        # Immediate children of src_dir, derived from gitea_files (one level).
        prefix = src_dir.rstrip("/") + "/"
        out: list[dict] = []
        seen_dirs: set[str] = set()
        for p in gitea_files:
            if not p.startswith(prefix):
                continue
            rest = p[len(prefix) :]
            if "/" in rest:
                d = prefix + rest.split("/")[0]
                if d not in seen_dirs:
                    seen_dirs.add(d)
                    out.append({"path": d, "type": "dir"})
            else:
                out.append({"path": p, "type": "file"})
        return out

    gitea.list_contents = AsyncMock(side_effect=_list_contents)
    stack.enter_context(patch("main.gitea_client", gitea))

    stack.enter_context(patch("main.resolve_job_repo", AsyncMock(return_value=repo)))

    db = MagicMock()
    db.update_job_exported_folder = AsyncMock(return_value=True)
    stack.enter_context(patch("main.postgres_db", db))

    return stack, backend, db


def _copied_paths(backend: FakeMainCloudBackend) -> set[str]:
    """Relative paths PUT into the session folder by the fake backend."""
    return {rel for (_native, rel) in backend._session_files}


# --------------------------------------------------------------------------- #
# _with_cloud_review_mode
# --------------------------------------------------------------------------- #


class TestCloudReviewMode:
    def test_open_folder_when_no_cloud_folder(self):
        out = main._with_cloud_review_mode(
            {"id": "x", "project_has_cloud_folder": False}
        )
        assert out["cloud_review_mode"] == "open_folder"
        assert "project_has_cloud_folder" not in out

    def test_diff_when_project_has_cloud_folder(self):
        out = main._with_cloud_review_mode(
            {"id": "x", "project_has_cloud_folder": True}
        )
        assert out["cloud_review_mode"] == "diff"
        assert "project_has_cloud_folder" not in out

    def test_open_folder_when_column_absent(self):
        # Loose jobs / rows without the join column default to open_folder.
        out = main._with_cloud_review_mode({"id": "x"})
        assert out["cloud_review_mode"] == "open_folder"

    def test_does_not_mutate_input(self):
        src = {"id": "x", "project_has_cloud_folder": True}
        main._with_cloud_review_mode(src)
        assert src["project_has_cloud_folder"] is True
        assert "cloud_review_mode" not in src


# --------------------------------------------------------------------------- #
# Routing gate
# --------------------------------------------------------------------------- #


class TestExportRoutingGate:
    @pytest.mark.asyncio
    async def test_rejects_project_with_cloud_folder(self, fake_request):
        user = _make_user()
        job = _make_job(project_id="p1", project_has_cloud_folder=True)
        stack, *_ = _patch_endpoint(
            user=user, job=job, backend=FakeMainCloudBackend(), gitea_files={}
        )
        with stack, pytest.raises(HTTPException) as ei:
            await main.export_job_to_shared_folder(fake_request, job["id"])
        assert ei.value.status_code == 409
        assert "diff-review" in ei.value.detail

    @pytest.mark.asyncio
    async def test_rejects_wrong_status(self, fake_request):
        user = _make_user()
        job = _make_job(status="processing")
        stack, *_ = _patch_endpoint(
            user=user, job=job, backend=FakeMainCloudBackend(), gitea_files={}
        )
        with stack, pytest.raises(HTTPException) as ei:
            await main.export_job_to_shared_folder(fake_request, job["id"])
        assert ei.value.status_code == 409

    @pytest.mark.asyncio
    async def test_allows_default_project_without_cloud_folder(self, fake_request):
        # The dead-zone case this change fixes: project_id is set (the
        # auto-assigned default project) but the project has no cloud folder.
        user = _make_user()
        job = _make_job(
            status="pending_review",
            project_id="default-proj",
            project_has_cloud_folder=False,
            freeze_data={"deliverables": ["spec.yaml"]},
        )
        stack, backend, db = _patch_endpoint(
            user=user,
            job=job,
            backend=FakeMainCloudBackend(),
            gitea_files={"spec.yaml": b"feature: x\n"},
        )
        with stack:
            result = await main.export_job_to_shared_folder(fake_request, job["id"])
        assert result["files_copied"] == 1
        assert db.update_job_exported_folder.await_count == 1

    @pytest.mark.asyncio
    async def test_allows_pending_review_status(self, fake_request):
        # In-review export = preview before approving.
        user = _make_user()
        job = _make_job(
            status="pending_review", freeze_data={"deliverables": ["spec.yaml"]}
        )
        stack, backend, _ = _patch_endpoint(
            user=user,
            job=job,
            backend=FakeMainCloudBackend(),
            gitea_files={"spec.yaml": b"x"},
        )
        with stack:
            result = await main.export_job_to_shared_folder(fake_request, job["id"])
        assert result["files_copied"] == 1


# --------------------------------------------------------------------------- #
# Deliverables copy
# --------------------------------------------------------------------------- #


class TestDeliverablesCopy:
    @pytest.mark.asyncio
    async def test_copies_declared_deliverables_preserving_paths(self, fake_request):
        user = _make_user()
        deliverables = [
            "spec.yaml",
            "spec_lock.md",
            "repo/src/app.py",
            "repo/tests/test_app.py",
            "repo/requirements.txt",
        ]
        job = _make_job(freeze_data={"deliverables": deliverables})
        gitea_files = {p: f"content of {p}".encode() for p in deliverables}
        # Undeclared scaffolding / metadata that lives in the repo but must NOT
        # be exported (this is what the old output/-only copy would have sent).
        gitea_files["output/job_frozen.json"] = b"{}"
        gitea_files["tools/x.py"] = b"scaffold"

        backend = FakeMainCloudBackend()
        stack, backend, _ = _patch_endpoint(
            user=user, job=job, backend=backend, gitea_files=gitea_files
        )
        with stack:
            result = await main.export_job_to_shared_folder(fake_request, job["id"])

        assert result["files_copied"] == 5
        copied = _copied_paths(backend)
        assert copied == set(deliverables)
        assert "output/job_frozen.json" not in copied
        assert "tools/x.py" not in copied
        # repo/ structure preserved verbatim.
        assert "repo/src/app.py" in copied

    @pytest.mark.asyncio
    async def test_skips_declared_deliverable_missing_in_repo(self, fake_request):
        user = _make_user()
        job = _make_job(freeze_data={"deliverables": ["output/report.md", "gone.txt"]})
        gitea_files = {"output/report.md": b"# report"}  # gone.txt absent
        backend = FakeMainCloudBackend()
        stack, backend, _ = _patch_endpoint(
            user=user, job=job, backend=backend, gitea_files=gitea_files
        )
        with stack:
            result = await main.export_job_to_shared_folder(fake_request, job["id"])
        assert result["files_copied"] == 1
        assert _copied_paths(backend) == {"output/report.md"}

    @pytest.mark.asyncio
    async def test_rejects_path_escape(self, fake_request):
        user = _make_user()
        job = _make_job(freeze_data={"deliverables": ["../secret.txt", "ok.md"]})
        gitea_files = {"ok.md": b"ok", "../secret.txt": b"nope"}
        backend = FakeMainCloudBackend()
        stack, backend, _ = _patch_endpoint(
            user=user, job=job, backend=backend, gitea_files=gitea_files
        )
        with stack:
            result = await main.export_job_to_shared_folder(fake_request, job["id"])
        assert result["files_copied"] == 1
        assert _copied_paths(backend) == {"ok.md"}

    @pytest.mark.asyncio
    async def test_falls_back_to_output_tree_when_no_deliverables(self, fake_request):
        user = _make_user()
        job = _make_job(freeze_data={"summary": "done"})  # no deliverables key
        gitea_files = {"output/a.md": b"a", "output/sub/b.md": b"b"}
        backend = FakeMainCloudBackend()
        stack, backend, _ = _patch_endpoint(
            user=user, job=job, backend=backend, gitea_files=gitea_files
        )
        with stack:
            result = await main.export_job_to_shared_folder(fake_request, job["id"])
        assert result["files_copied"] == 2
        assert _copied_paths(backend) == {"output/a.md", "output/sub/b.md"}

    @pytest.mark.asyncio
    async def test_freeze_data_as_json_string(self, fake_request):
        # asyncpg may hand back JSONB as a str — the endpoint must json.loads it.
        user = _make_user()
        job = _make_job(freeze_data=json.dumps({"deliverables": ["spec.yaml"]}))
        backend = FakeMainCloudBackend()
        stack, backend, _ = _patch_endpoint(
            user=user, job=job, backend=backend, gitea_files={"spec.yaml": b"x"}
        )
        with stack:
            result = await main.export_job_to_shared_folder(fake_request, job["id"])
        assert result["files_copied"] == 1
        assert _copied_paths(backend) == {"spec.yaml"}
