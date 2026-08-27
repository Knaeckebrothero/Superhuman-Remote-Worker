"""Fail-closed project-cloud delivery for isolated loop jobs."""

from __future__ import annotations

import base64
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.cloud import (
    CloudBackendError,
    CloudBackendErrorKind,
    ProjectFolderEntry,
)
from services.job_cloud_baseline import (
    apply_diff_to_cloud,
    deliver_loop_diff_to_cloud,
    detect_external_mods,
    project_folder_slug,
    seed_project_folder_baseline,
)


BASE = "a" * 40
HEAD = "b" * 40


def _job(**over):
    row = {
        "id": "abcdef12-3456-7890-abcd-ef1234567890",
        "repo_name": "job-abcdef12",
        "branch_name": None,
        "cloud_diff_baseline_commit": BASE,
        "context": {"cloud_baseline": {"entries": {"report.md": "etag-before"}}},
    }
    row.update(over)
    return row


def _gitea(*, changed: bool = True):
    gitea = MagicMock()
    gitea.get_branch_head_sha = AsyncMock(return_value=HEAD)
    base_tree = [
        {"path": "projects/my_project/report.md", "type": "blob", "sha": "old"}
    ]
    head_tree = [
        {
            "path": "projects/my_project/report.md",
            "type": "blob",
            "sha": "new" if changed else "old",
        }
    ]
    gitea.list_tree = AsyncMock(
        side_effect=lambda _repo, ref: base_tree if ref == BASE else head_tree
    )
    return gitea


def _db():
    db = MagicMock()
    db.update_job_cloud_diff = AsyncMock(return_value=True)
    return db


PROJECT = {
    "name": "My Project",
    "main_cloud_backend": "opencloud",
    "main_cloud_folder_handle": "opaque",
}


@pytest.mark.asyncio
async def test_no_project_file_changes_advances_without_cloud_write():
    db = _db()
    with (
        patch(
            "services.job_cloud_baseline.detect_external_mods", new_callable=AsyncMock
        ) as detect,
        patch(
            "services.job_cloud_baseline.apply_diff_to_cloud", new_callable=AsyncMock
        ) as apply,
    ):
        outcome = await deliver_loop_diff_to_cloud(
            job=_job(),
            project=PROJECT,
            postgres_db=db,
            gitea_client=_gitea(changed=False),
            main_cloud_router=MagicMock(),
        )

    assert outcome["delivery_status"] == "no-changes"
    assert outcome["needs_review"] is False
    detect.assert_not_awaited()
    apply.assert_not_awaited()
    db.update_job_cloud_diff.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_project_repo_changes_are_not_reported_as_cloud_applied():
    gitea = _gitea()
    gitea.list_tree = AsyncMock(
        side_effect=lambda _repo, ref: (
            []
            if ref == BASE
            else [{"path": "output/run-notes.md", "type": "blob", "sha": "new"}]
        )
    )
    db = _db()
    with (
        patch(
            "services.job_cloud_baseline.detect_external_mods",
            new_callable=AsyncMock,
        ) as detect,
        patch(
            "services.job_cloud_baseline.apply_diff_to_cloud",
            new_callable=AsyncMock,
        ) as apply,
    ):
        outcome = await deliver_loop_diff_to_cloud(
            job=_job(),
            project=PROJECT,
            postgres_db=db,
            gitea_client=gitea,
            main_cloud_router=MagicMock(),
        )

    assert outcome["delivery_status"] == "no-changes"
    assert outcome["needs_review"] is False
    detect.assert_not_awaited()
    apply.assert_not_awaited()
    db.update_job_cloud_diff.assert_not_awaited()


@pytest.mark.asyncio
async def test_clean_diff_is_applied_and_accepted():
    job = _job()
    db = _db()
    with (
        patch(
            "services.job_cloud_baseline.detect_external_mods",
            AsyncMock(return_value=[]),
        ) as detect,
        patch(
            "services.job_cloud_baseline.apply_diff_to_cloud",
            AsyncMock(return_value={"applied": 1, "deleted": 0, "errors": []}),
        ) as apply,
    ):
        outcome = await deliver_loop_diff_to_cloud(
            job=job,
            project=PROJECT,
            postgres_db=db,
            gitea_client=_gitea(),
            main_cloud_router=MagicMock(),
        )

    assert outcome == {
        "delivery_status": "cloud-applied",
        "needs_review": False,
        "delivery_sha": HEAD,
        "notes": [],
        "applied": 1,
        "deleted": 0,
    }
    assert [
        call.kwargs["diff_status"] for call in db.update_job_cloud_diff.await_args_list
    ] == [
        "pending",
        "accepted",
    ]
    assert job["diff_status"] == "accepted"
    assert detect.await_args.kwargs["scope_paths"] == {"report.md"}
    assert detect.await_args.kwargs["strict"] is True
    apply.assert_awaited_once()


@pytest.mark.asyncio
async def test_completion_command_registers_bounded_intent_before_cloud_apply():
    job = _job()
    db = _db()
    db.merge_job_context = AsyncMock()
    order: list[str] = []

    async def clean(*_args, **_kwargs):
        order.append("divergence-check")
        return []

    async def register(*_args, **_kwargs):
        order.append("intent")
        return True

    async def apply(*_args, **_kwargs):
        order.append("apply")
        return {"applied": 1, "deleted": 0, "errors": []}

    db.merge_job_context.side_effect = register
    with (
        patch("services.job_cloud_baseline.detect_external_mods", side_effect=clean),
        patch("services.job_cloud_baseline.apply_diff_to_cloud", side_effect=apply),
    ):
        outcome = await deliver_loop_diff_to_cloud(
            job=job,
            project=PROJECT,
            postgres_db=db,
            gitea_client=_gitea(),
            main_cloud_router=MagicMock(),
            completion_command_id="command-1",
        )

    assert outcome["delivery_status"] == "cloud-applied"
    assert order == ["divergence-check", "intent", "apply"]
    db.merge_job_context.assert_awaited_once_with(
        str(job["id"]),
        {
            "loop_cloud_delivery": {
                "delivery_status": "cloud-applying",
                "completion_command_id": "command-1",
                "baseline_commit": BASE,
                "delivery_sha": HEAD,
            }
        },
    )


@pytest.mark.asyncio
async def test_same_command_reapplies_after_cloud_apply_before_db_stamp():
    job = _job()
    db = _db()
    db.merge_job_context = AsyncMock()

    async def persist_intent(_job_id, patch):
        context = dict(job["context"])
        context.update(patch)
        job["context"] = context
        return True

    db.merge_job_context.side_effect = persist_intent
    db.update_job_cloud_diff.side_effect = [
        True,
        RuntimeError("crash after WebDAV apply"),
        True,
        True,
    ]
    detect = AsyncMock(return_value=[])
    apply = AsyncMock(return_value={"applied": 1, "deleted": 0, "errors": []})

    with (
        patch("services.job_cloud_baseline.detect_external_mods", detect),
        patch("services.job_cloud_baseline.apply_diff_to_cloud", apply),
    ):
        with pytest.raises(RuntimeError, match="crash after WebDAV apply"):
            await deliver_loop_diff_to_cloud(
                job=job,
                project=PROJECT,
                postgres_db=db,
                gitea_client=_gitea(),
                main_cloud_router=MagicMock(),
                completion_command_id="command-1",
            )

        outcome = await deliver_loop_diff_to_cloud(
            job=job,
            project=PROJECT,
            postgres_db=db,
            gitea_client=_gitea(),
            main_cloud_router=MagicMock(),
            completion_command_id="command-1",
        )

    assert outcome["delivery_status"] == "cloud-applied"
    assert detect.await_count == 1
    assert apply.await_count == 2
    assert db.merge_job_context.await_count == 1


@pytest.mark.asyncio
async def test_cloud_apply_does_not_start_when_command_intent_is_not_durable():
    job = _job()
    db = _db()
    db.merge_job_context = AsyncMock(return_value=False)
    detect = AsyncMock(return_value=[])
    apply = AsyncMock()

    with (
        patch("services.job_cloud_baseline.detect_external_mods", detect),
        patch("services.job_cloud_baseline.apply_diff_to_cloud", apply),
    ):
        with pytest.raises(RuntimeError, match="could not persist"):
            await deliver_loop_diff_to_cloud(
                job=job,
                project=PROJECT,
                postgres_db=db,
                gitea_client=_gitea(),
                main_cloud_router=MagicMock(),
                completion_command_id="command-1",
            )

    detect.assert_awaited_once()
    apply.assert_not_awaited()


@pytest.mark.asyncio
async def test_different_command_cannot_adopt_cloud_apply_intent():
    job = _job(
        context={
            "cloud_baseline": {"entries": {"report.md": "etag-before"}},
            "loop_cloud_delivery": {
                "delivery_status": "cloud-applying",
                "completion_command_id": "old-command",
                "baseline_commit": BASE,
                "delivery_sha": HEAD,
            },
        }
    )
    db = _db()
    db.merge_job_context = AsyncMock()
    diverged = [{"path": "report.md", "kind": "etag_mismatch"}]
    detect = AsyncMock(return_value=diverged)
    apply = AsyncMock()

    with (
        patch("services.job_cloud_baseline.detect_external_mods", detect),
        patch("services.job_cloud_baseline.apply_diff_to_cloud", apply),
    ):
        outcome = await deliver_loop_diff_to_cloud(
            job=job,
            project=PROJECT,
            postgres_db=db,
            gitea_client=_gitea(),
            main_cloud_router=MagicMock(),
            completion_command_id="new-command",
        )

    assert outcome["delivery_status"] == "cloud-conflict"
    assert outcome["diverged"] == diverged
    detect.assert_awaited_once()
    apply.assert_not_awaited()
    db.merge_job_context.assert_not_awaited()


@pytest.mark.asyncio
async def test_external_conflict_requires_review_and_does_not_apply():
    job = _job()
    db = _db()
    diverged = [{"path": "report.md", "kind": "etag_mismatch"}]
    with (
        patch(
            "services.job_cloud_baseline.detect_external_mods",
            AsyncMock(return_value=diverged),
        ),
        patch(
            "services.job_cloud_baseline.apply_diff_to_cloud", new_callable=AsyncMock
        ) as apply,
    ):
        outcome = await deliver_loop_diff_to_cloud(
            job=job,
            project=PROJECT,
            postgres_db=db,
            gitea_client=_gitea(),
            main_cloud_router=MagicMock(),
        )

    assert outcome["delivery_status"] == "cloud-conflict"
    assert outcome["needs_review"] is True
    assert outcome["diverged"] == diverged
    assert job["diff_status"] == "pending"
    apply.assert_not_awaited()


@pytest.mark.asyncio
async def test_partial_cloud_write_requires_review():
    job = _job()
    db = _db()
    with (
        patch(
            "services.job_cloud_baseline.detect_external_mods",
            AsyncMock(return_value=[]),
        ),
        patch(
            "services.job_cloud_baseline.apply_diff_to_cloud",
            AsyncMock(
                return_value={
                    "applied": 1,
                    "deleted": 0,
                    "errors": ["projects/my_project/other.md: backend down"],
                }
            ),
        ),
    ):
        outcome = await deliver_loop_diff_to_cloud(
            job=job,
            project=PROJECT,
            postgres_db=db,
            gitea_client=_gitea(),
            main_cloud_router=MagicMock(),
        )

    assert outcome["delivery_status"] == "cloud-partial"
    assert outcome["needs_review"] is True
    assert outcome["applied"] == 1
    assert job["diff_status"] == "pending"


@pytest.mark.asyncio
async def test_missing_baseline_fails_closed():
    outcome = await deliver_loop_diff_to_cloud(
        job=_job(cloud_diff_baseline_commit=None),
        project=PROJECT,
        postgres_db=_db(),
        gitea_client=_gitea(),
        main_cloud_router=MagicMock(),
    )

    assert outcome["delivery_status"] == "cloud-unavailable"
    assert outcome["needs_review"] is True


@pytest.mark.asyncio
async def test_already_applied_callback_is_idempotent():
    gitea = _gitea()
    job = _job(
        merge_status="cloud-applied",
        diff_status="accepted",
        context={
            "cloud_baseline": {"entries": {"report.md": "etag-before"}},
            "loop_cloud_delivery": {
                "delivery_status": "cloud-applied",
                "delivery_sha": HEAD,
                "notes": [],
                "applied": 1,
                "deleted": 0,
            },
        },
    )

    outcome = await deliver_loop_diff_to_cloud(
        job=job,
        project=PROJECT,
        postgres_db=_db(),
        gitea_client=gitea,
        main_cloud_router=MagicMock(),
    )

    assert outcome["delivery_status"] == "cloud-applied"
    assert outcome["needs_review"] is False
    assert outcome["delivery_sha"] == HEAD
    gitea.get_branch_head_sha.assert_not_awaited()


@pytest.mark.asyncio
async def test_strict_conflict_check_does_not_treat_listing_failure_as_clean():
    backend = MagicMock()
    backend.is_initialized = True
    backend.list_project_folder = AsyncMock(
        side_effect=CloudBackendError(
            CloudBackendErrorKind.UNAVAILABLE,
            "PROPFIND unavailable",
            backend="opencloud",
        )
    )
    router = MagicMock()
    router.for_backend.return_value = backend

    with pytest.raises(CloudBackendError):
        await detect_external_mods(
            job=_job(),
            project=PROJECT,
            main_cloud_router=router,
            scope_paths={"report.md"},
            strict=True,
        )


@pytest.mark.asyncio
async def test_strict_loop_baseline_seeds_binary_files_byte_for_byte():
    raw = b"%PDF-\x00\xff\x10"
    backend = MagicMock()
    backend.is_initialized = True
    backend.list_project_folder = AsyncMock(
        return_value=[
            ProjectFolderEntry(
                path="deliverables/report.pdf",
                is_dir=False,
                size=len(raw),
                etag="pdf-etag",
            )
        ]
    )
    backend.get_project_folder_file_bytes = AsyncMock(return_value=raw)
    router = MagicMock()
    router.for_backend.return_value = backend

    db = MagicMock()
    db.get_job = AsyncMock(return_value={})
    db.merge_job_context = AsyncMock(return_value=True)
    db.update_job_cloud_diff = AsyncMock(return_value=True)

    gitea = MagicMock()
    gitea.change_files = AsyncMock(return_value=True)
    gitea.create_or_update_file = AsyncMock(return_value=True)
    gitea.get_branch_head_sha = AsyncMock(return_value=BASE)

    await seed_project_folder_baseline(
        job_id=str(_job()["id"]),
        project=PROJECT,
        repo_name="job-abcdef12",
        branch=None,
        postgres_db=db,
        gitea_client=gitea,
        main_cloud_router=router,
        require_complete=True,
    )

    gitea.create_or_update_file.assert_not_awaited()
    binary_change = gitea.change_files.await_args
    assert binary_change.args[:2] == ("job-abcdef12", "main")
    assert binary_change.args[2] == [
        {
            "path": "projects/my_project/deliverables/report.pdf",
            "content_b64": base64.b64encode(raw).decode("ascii"),
        }
    ]
    db.update_job_cloud_diff.assert_awaited_once_with(
        str(_job()["id"]), baseline_commit=BASE
    )


@pytest.mark.asyncio
async def test_cloud_apply_preserves_binary_job_artifact_bytes():
    raw = b"\x89PNG\r\n\x1a\n\x00\xff"
    gitea = MagicMock()
    gitea.get_branch_head_sha = AsyncMock(return_value=HEAD)
    gitea.list_tree = AsyncMock(
        side_effect=lambda _repo, ref: (
            []
            if ref == BASE
            else [
                {
                    "path": "projects/my_project/output/chart.png",
                    "type": "blob",
                    "sha": "image-blob",
                }
            ]
        )
    )
    gitea.get_file_bytes = AsyncMock(return_value=raw)

    backend = MagicMock()
    backend.is_initialized = True
    backend.put_project_folder_file_bytes = AsyncMock()
    router = MagicMock()
    router.for_backend.return_value = backend

    result = await apply_diff_to_cloud(
        job=_job(),
        project=PROJECT,
        gitea_client=gitea,
        main_cloud_router=router,
    )

    assert result == {"applied": 1, "deleted": 0, "errors": []}
    backend.put_project_folder_file_bytes.assert_awaited_once()
    assert backend.put_project_folder_file_bytes.await_args.kwargs == {
        "path": "output/chart.png",
        "content": raw,
    }


def test_seed_time_slug_survives_project_rename():
    job = _job(
        context={
            "cloud_baseline": {
                "project_slug": "original_project",
                "entries": {},
            }
        }
    )
    assert project_folder_slug(job, {"name": "Renamed Project"}) == "original_project"


@pytest.mark.asyncio
async def test_idempotent_seed_preserves_etags_and_seed_time_slug():
    job = _job(
        cloud_diff_baseline_commit=BASE,
        context={
            "cloud_baseline": {
                "state": "ready",
                "project_slug": "original_project",
                "entries": {"report.md": "etag-before"},
            }
        },
    )
    db = MagicMock()
    db.get_job = AsyncMock(return_value=job)
    db.merge_job_context = AsyncMock(return_value=True)
    gitea = MagicMock()

    await seed_project_folder_baseline(
        job_id=str(job["id"]),
        project={**PROJECT, "name": "Renamed Project"},
        repo_name="job-abcdef12",
        branch=None,
        postgres_db=db,
        gitea_client=gitea,
        main_cloud_router=MagicMock(),
        require_complete=True,
    )

    db.merge_job_context.assert_awaited_once_with(
        str(job["id"]),
        {
            "cloud_baseline": {
                "state": "ready",
                "project_slug": "original_project",
                "entries": {"report.md": "etag-before"},
            }
        },
    )
    gitea.change_files.assert_not_called()
