"""Tests for the per-job repo model (resolve_job_repo, _squash_merge_subjob, etc.)."""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

# Add orchestrator/ to sys.path so its internal imports resolve
_orch_dir = str(Path(__file__).parent.parent / "orchestrator")
if _orch_dir not in sys.path:
    sys.path.insert(0, _orch_dir)

import main as orch_main  # noqa: E402

MODULE = "main"


# ===========================================================================
# resolve_job_repo
# ===========================================================================


class TestResolveJobRepo:
    """Tests for resolve_job_repo()."""

    @pytest.mark.asyncio
    async def test_job_not_found_raises_404(self):
        with patch(f"{MODULE}.postgres_db") as mock_db:
            mock_db.get_job = AsyncMock(return_value=None)

            with pytest.raises(HTTPException) as exc_info:
                await orch_main.resolve_job_repo("nonexistent-id")
            assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_root_job_with_repo_name(self):
        """Root job has repo_name stored — should return it directly."""
        job = {"repo_name": "job-abcd1234", "branch_name": None}
        with patch(f"{MODULE}.postgres_db") as mock_db:
            mock_db.get_job = AsyncMock(return_value=job)

            repo, branch = await orch_main.resolve_job_repo("abcd1234-xxxx")
            assert repo == "job-abcd1234"
            assert branch is None

    @pytest.mark.asyncio
    async def test_subjob_with_repo_name(self):
        """Subjob has repo_name + branch_name stored on itself."""
        job = {
            "repo_name": "job-parent12",
            "branch_name": "subjob/abcd1234/creator",
            "parent_job_id": "parent-uuid",
        }
        with patch(f"{MODULE}.postgres_db") as mock_db:
            mock_db.get_job = AsyncMock(return_value=job)

            repo, branch = await orch_main.resolve_job_repo("abcd1234-xxxx")
            assert repo == "job-parent12"
            assert branch == "subjob/abcd1234/creator"

    @pytest.mark.asyncio
    async def test_subjob_traverses_to_parent(self):
        """Subjob without repo_name traverses parent_job_id to find repo."""
        subjob = {
            "repo_name": None,
            "branch_name": "subjob/sub12345/validator",
            "parent_job_id": "parent-uuid",
        }
        parent = {"repo_name": "job-parent12", "branch_name": None}

        with patch(f"{MODULE}.postgres_db") as mock_db:
            mock_db.get_job = AsyncMock(
                side_effect=lambda jid: {
                    "sub12345-xxxx": subjob,
                    "parent-uuid": parent,
                }.get(jid)
            )

            repo, branch = await orch_main.resolve_job_repo("sub12345-xxxx")
            assert repo == "job-parent12"
            assert branch == "subjob/sub12345/validator"

    @pytest.mark.asyncio
    async def test_legacy_project_jobs_repo_fallback(self):
        """Pre-migration job with project_id falls back to project jobs repo."""
        job = {
            "repo_name": None,
            "branch_name": "job/legacy-branch",
            "parent_job_id": None,
            "project_id": "proj-uuid",
        }

        with patch(f"{MODULE}.postgres_db") as mock_db:
            mock_db.get_job = AsyncMock(return_value=job)
            mock_db.get_project_repositories = AsyncMock(
                return_value=[{"name": "my-project-jobs"}]
            )

            repo, branch = await orch_main.resolve_job_repo("legacy-id")
            assert repo == "my-project-jobs"
            assert branch == "job/legacy-branch"
            mock_db.get_project_repositories.assert_awaited_once_with(
                "proj-uuid", role="jobs"
            )

    @pytest.mark.asyncio
    async def test_non_project_legacy_fallback(self):
        """Job with no repo_name, no parent, no project falls back to job-{id}."""
        job = {
            "repo_name": None,
            "branch_name": None,
            "parent_job_id": None,
            "project_id": None,
        }

        with patch(f"{MODULE}.postgres_db") as mock_db:
            mock_db.get_job = AsyncMock(return_value=job)

            repo, branch = await orch_main.resolve_job_repo("full-uuid-here")
            assert repo == "job-full-uuid-here"
            assert branch is None


# ===========================================================================
# _squash_merge_subjob
# ===========================================================================


class TestSquashMergeSubjob:
    """Tests for _squash_merge_subjob()."""

    @pytest.mark.asyncio
    async def test_returns_none_for_non_subjob(self):
        """Root job (no parent_job_id) should return None."""
        with patch(f"{MODULE}.postgres_db") as mock_db:
            mock_db.get_job = AsyncMock(return_value={"parent_job_id": None})

            result = await orch_main._squash_merge_subjob("root-job-id")
            assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_job_not_found(self):
        with patch(f"{MODULE}.postgres_db") as mock_db:
            mock_db.get_job = AsyncMock(return_value=None)

            result = await orch_main._squash_merge_subjob("missing-id")
            assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_without_branch_or_repo(self):
        """Subjob without branch/repo should be skipped."""
        job = {"parent_job_id": "parent-id", "branch_name": None, "repo_name": None}
        with patch(f"{MODULE}.postgres_db") as mock_db:
            mock_db.get_job = AsyncMock(return_value=job)

            result = await orch_main._squash_merge_subjob("subjob-id")
            assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_gitea_not_initialized(self):
        job = {
            "parent_job_id": "parent-id",
            "branch_name": "subjob/abc/creator",
            "repo_name": "job-parent",
        }
        with (
            patch(f"{MODULE}.postgres_db") as mock_db,
            patch(f"{MODULE}.gitea_client") as mock_gitea,
        ):
            mock_db.get_job = AsyncMock(return_value=job)
            mock_gitea.is_initialized = False

            result = await orch_main._squash_merge_subjob("subjob-id")
            assert result is None

    @pytest.mark.asyncio
    async def test_successful_squash_merge(self):
        """Full happy-path: cleanup -> PR -> merge -> status update."""
        subjob = {
            "id": "subjob-uuid",
            "parent_job_id": "parent-uuid",
            "branch_name": "subjob/abcd1234/creator",
            "repo_name": "job-parent12",
            "config_name": "creator",
            "description": "Do something important",
        }
        parent = {"branch_name": None}  # root parent -> base = "main"

        with (
            patch(f"{MODULE}.postgres_db") as mock_db,
            patch(f"{MODULE}.gitea_client") as mock_gitea,
        ):
            mock_db.get_job = AsyncMock(
                side_effect=lambda jid: {
                    "subjob-uuid": subjob,
                    "parent-uuid": parent,
                }.get(jid)
            )
            mock_db.update_job_merge_status = AsyncMock()

            mock_gitea.is_initialized = True
            mock_gitea.delete_file = AsyncMock()
            mock_gitea.list_contents = AsyncMock(return_value=[])
            mock_gitea.create_pr = AsyncMock(return_value={"number": 42})
            mock_gitea.merge_pr = AsyncMock(return_value=True)

            result = await orch_main._squash_merge_subjob("subjob-uuid")

            assert result == {
                "status": "merged",
                "pr_number": 42,
                "base_branch": "main",
            }

            # Verify cleanup files were deleted
            assert mock_gitea.delete_file.await_count == len(
                orch_main.SUBJOB_CLEANUP_FILES
            )

            # Verify PR created with squash
            mock_gitea.create_pr.assert_awaited_once()
            mock_gitea.merge_pr.assert_awaited_once_with(
                "job-parent12",
                42,
                merge_strategy="squash",
                delete_branch_after_merge=True,
            )

            mock_db.update_job_merge_status.assert_awaited_once_with(
                "subjob-uuid", merge_status="merged"
            )

    @pytest.mark.asyncio
    async def test_pr_creation_returns_none_skips_merge(self):
        """When PR creation returns None (no changes), merge is skipped."""
        subjob = {
            "id": "subjob-uuid",
            "parent_job_id": "parent-uuid",
            "branch_name": "subjob/abcd1234/creator",
            "repo_name": "job-parent12",
            "config_name": "creator",
            "description": "Task",
        }
        parent = {"branch_name": None}

        with (
            patch(f"{MODULE}.postgres_db") as mock_db,
            patch(f"{MODULE}.gitea_client") as mock_gitea,
        ):
            mock_db.get_job = AsyncMock(
                side_effect=lambda jid: {
                    "subjob-uuid": subjob,
                    "parent-uuid": parent,
                }.get(jid)
            )
            mock_db.update_job_merge_status = AsyncMock()

            mock_gitea.is_initialized = True
            mock_gitea.delete_file = AsyncMock()
            mock_gitea.list_contents = AsyncMock(return_value=[])
            mock_gitea.create_pr = AsyncMock(return_value=None)

            result = await orch_main._squash_merge_subjob("subjob-uuid")

            assert result == {"status": "skipped", "reason": "no changes"}
            mock_db.update_job_merge_status.assert_awaited_once_with(
                "subjob-uuid", merge_status="skipped"
            )
            mock_gitea.merge_pr.assert_not_called()

    @pytest.mark.asyncio
    async def test_merge_failure_returns_conflict(self):
        """When merge_pr returns False, status is conflict."""
        subjob = {
            "id": "sub-id",
            "parent_job_id": "parent-id",
            "branch_name": "subjob/sub12345/validator",
            "repo_name": "job-parent12",
            "config_name": "validator",
            "description": "Verify",
        }
        parent = {"branch_name": "subjob/parent-br/creator"}

        with (
            patch(f"{MODULE}.postgres_db") as mock_db,
            patch(f"{MODULE}.gitea_client") as mock_gitea,
        ):
            mock_db.get_job = AsyncMock(
                side_effect=lambda jid: {
                    "sub-id": subjob,
                    "parent-id": parent,
                }.get(jid)
            )
            mock_db.update_job_merge_status = AsyncMock()

            mock_gitea.is_initialized = True
            mock_gitea.delete_file = AsyncMock()
            mock_gitea.list_contents = AsyncMock(return_value=[])
            mock_gitea.create_pr = AsyncMock(return_value={"number": 99})
            mock_gitea.merge_pr = AsyncMock(return_value=False)

            result = await orch_main._squash_merge_subjob("sub-id")

            assert result == {"status": "conflict", "pr_number": 99}
            mock_db.update_job_merge_status.assert_awaited_once_with(
                "sub-id", merge_status="conflict"
            )

    @pytest.mark.asyncio
    async def test_directory_cleanup_deletes_files_in_dirs(self):
        """Cleanup dirs: list_contents -> delete each file entry."""
        subjob = {
            "id": "sub-id",
            "parent_job_id": "parent-id",
            "branch_name": "subjob/sub12345/creator",
            "repo_name": "job-parent12",
            "config_name": "creator",
            "description": "Work",
        }
        parent = {"branch_name": None}

        dir_entries = [
            {"type": "file", "path": "archive/todos_phase_1.yaml"},
            {"type": "file", "path": "archive/phase_1_retrospective.md"},
            {"type": "dir", "path": "archive/subdir"},  # dirs should be skipped
        ]

        with (
            patch(f"{MODULE}.postgres_db") as mock_db,
            patch(f"{MODULE}.gitea_client") as mock_gitea,
        ):
            mock_db.get_job = AsyncMock(
                side_effect=lambda jid: {
                    "sub-id": subjob,
                    "parent-id": parent,
                }.get(jid)
            )
            mock_db.update_job_merge_status = AsyncMock()

            mock_gitea.is_initialized = True
            mock_gitea.delete_file = AsyncMock()
            # Return dir_entries only for "archive", empty for other dirs
            mock_gitea.list_contents = AsyncMock(
                side_effect=lambda repo, path, ref=None: dir_entries
                if path == "archive"
                else []
            )
            mock_gitea.create_pr = AsyncMock(return_value={"number": 1})
            mock_gitea.merge_pr = AsyncMock(return_value=True)

            await orch_main._squash_merge_subjob("sub-id")

            # Count: SUBJOB_CLEANUP_FILES (individual files) + 2 file entries from archive dir
            total_delete_calls = len(orch_main.SUBJOB_CLEANUP_FILES) + 2
            assert mock_gitea.delete_file.await_count == total_delete_calls

    @pytest.mark.asyncio
    async def test_base_branch_from_parent_branch(self):
        """When parent has a branch_name, squash merges into that branch."""
        subjob = {
            "id": "sub-id",
            "parent_job_id": "parent-id",
            "branch_name": "subjob/sub12345/validator",
            "repo_name": "job-parent12",
            "config_name": "validator",
            "description": "Check",
        }
        parent = {"branch_name": "subjob/parent-br/creator"}

        with (
            patch(f"{MODULE}.postgres_db") as mock_db,
            patch(f"{MODULE}.gitea_client") as mock_gitea,
        ):
            mock_db.get_job = AsyncMock(
                side_effect=lambda jid: {
                    "sub-id": subjob,
                    "parent-id": parent,
                }.get(jid)
            )
            mock_db.update_job_merge_status = AsyncMock()

            mock_gitea.is_initialized = True
            mock_gitea.delete_file = AsyncMock()
            mock_gitea.list_contents = AsyncMock(return_value=[])
            mock_gitea.create_pr = AsyncMock(return_value={"number": 5})
            mock_gitea.merge_pr = AsyncMock(return_value=True)

            result = await orch_main._squash_merge_subjob("sub-id")

            assert result["base_branch"] == "subjob/parent-br/creator"
            # Verify PR was created against parent's branch
            call_kwargs = mock_gitea.create_pr.call_args
            assert call_kwargs.kwargs["base"] == "subjob/parent-br/creator"


# ===========================================================================
# delete_job Gitea cleanup
# ===========================================================================


class TestDeleteJobGiteaCleanup:
    """Tests for delete_job() Gitea branch/repo cleanup logic."""

    @pytest.mark.asyncio
    async def test_root_job_deletes_repo(self):
        """Root job with repo_name: deletes the entire Gitea repo."""
        job = {
            "repo_name": "job-abcd1234",
            "branch_name": None,
            "parent_job_id": None,
            "project_id": None,
        }

        with (
            patch(f"{MODULE}.postgres_db") as mock_db,
            patch(f"{MODULE}.gitea_client") as mock_gitea,
        ):
            mock_db.get_job = AsyncMock(return_value=job)
            mock_db.delete_job = AsyncMock(return_value=True)
            mock_gitea.is_initialized = True
            mock_gitea.delete_repo = AsyncMock()
            mock_gitea.delete_branch = AsyncMock()

            result = await orch_main.delete_job("abcd1234-xxxx")

            assert result == {"status": "deleted"}
            mock_gitea.delete_repo.assert_awaited_once_with("job-abcd1234")
            mock_gitea.delete_branch.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_subjob_deletes_branch(self):
        """Subjob with branch_name + repo_name: deletes branch only."""
        job = {
            "repo_name": "job-parent12",
            "branch_name": "subjob/abcd1234/creator",
            "parent_job_id": "parent-uuid",
            "project_id": None,
        }

        with (
            patch(f"{MODULE}.postgres_db") as mock_db,
            patch(f"{MODULE}.gitea_client") as mock_gitea,
        ):
            mock_db.get_job = AsyncMock(return_value=job)
            mock_db.delete_job = AsyncMock(return_value=True)
            mock_gitea.is_initialized = True
            mock_gitea.delete_branch = AsyncMock()
            mock_gitea.delete_repo = AsyncMock()

            result = await orch_main.delete_job("abcd1234-xxxx")

            assert result == {"status": "deleted"}
            mock_gitea.delete_branch.assert_awaited_once_with(
                "job-parent12", "subjob/abcd1234/creator"
            )
            mock_gitea.delete_repo.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_legacy_project_job_deletes_branch(self):
        """Legacy job with project_id + branch_name but no repo_name."""
        job = {
            "repo_name": None,
            "branch_name": "job/legacy-branch",
            "parent_job_id": None,
            "project_id": "proj-uuid",
        }

        with (
            patch(f"{MODULE}.postgres_db") as mock_db,
            patch(f"{MODULE}.gitea_client") as mock_gitea,
        ):
            mock_db.get_job = AsyncMock(return_value=job)
            mock_db.delete_job = AsyncMock(return_value=True)
            mock_db.get_project_repositories = AsyncMock(
                return_value=[{"name": "project-jobs-repo"}]
            )
            mock_gitea.is_initialized = True
            mock_gitea.delete_branch = AsyncMock()
            mock_gitea.delete_repo = AsyncMock()

            result = await orch_main.delete_job("legacy-id")

            assert result == {"status": "deleted"}
            mock_gitea.delete_branch.assert_awaited_once_with(
                "project-jobs-repo", "job/legacy-branch"
            )

    @pytest.mark.asyncio
    async def test_gitea_not_initialized_skips_cleanup(self):
        """When Gitea is not initialized, cleanup is skipped entirely."""
        job = {"repo_name": "job-abc", "branch_name": None, "parent_job_id": None}

        with (
            patch(f"{MODULE}.postgres_db") as mock_db,
            patch(f"{MODULE}.gitea_client") as mock_gitea,
        ):
            mock_db.get_job = AsyncMock(return_value=job)
            mock_db.delete_job = AsyncMock(return_value=True)
            mock_gitea.is_initialized = False

            result = await orch_main.delete_job("some-id")

            assert result == {"status": "deleted"}
            mock_gitea.delete_repo.assert_not_called()
            mock_gitea.delete_branch.assert_not_called()

    @pytest.mark.asyncio
    async def test_job_not_found_raises_404(self):
        with patch(f"{MODULE}.postgres_db") as mock_db:
            mock_db.get_job = AsyncMock(return_value=None)

            with pytest.raises(HTTPException) as exc_info:
                await orch_main.delete_job("missing-id")
            assert exc_info.value.status_code == 404


# ===========================================================================
# subjob_merge endpoint
# ===========================================================================


class TestSubjobMergeEndpoint:
    """Tests for POST /api/jobs/{job_id}/subjob-merge."""

    @pytest.mark.asyncio
    async def test_rejects_non_subjob(self):
        """Root jobs (no parent_job_id) should be rejected with 400."""
        job = {"parent_job_id": None}

        with patch(f"{MODULE}.postgres_db") as mock_db:
            mock_db.get_job = AsyncMock(return_value=job)

            with pytest.raises(HTTPException) as exc_info:
                await orch_main.subjob_merge("root-job-id")
            assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_returns_404_for_missing_job(self):
        with patch(f"{MODULE}.postgres_db") as mock_db:
            mock_db.get_job = AsyncMock(return_value=None)

            with pytest.raises(HTTPException) as exc_info:
                await orch_main.subjob_merge("missing-id")
            assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_returns_skipped_when_no_branch(self):
        """Subjob without branch config returns skipped."""
        job = {"parent_job_id": "parent-id", "branch_name": None, "repo_name": None}

        with (
            patch(f"{MODULE}.postgres_db") as mock_db,
            patch(f"{MODULE}.gitea_client") as mock_gitea,
        ):
            mock_db.get_job = AsyncMock(return_value=job)
            mock_gitea.is_initialized = True

            result = await orch_main.subjob_merge("subjob-id")
            assert result["status"] == "skipped"

    @pytest.mark.asyncio
    async def test_returns_merge_result(self):
        """Happy path: returns merged result from _squash_merge_subjob."""
        job = {
            "parent_job_id": "parent-id",
            "branch_name": "subjob/abc/creator",
            "repo_name": "job-parent",
        }

        with (
            patch(f"{MODULE}.postgres_db") as mock_db,
            patch(
                f"{MODULE}._squash_merge_subjob", new_callable=AsyncMock
            ) as mock_merge,
        ):
            mock_db.get_job = AsyncMock(return_value=job)
            mock_merge.return_value = {
                "status": "merged",
                "pr_number": 42,
                "base_branch": "main",
            }

            result = await orch_main.subjob_merge("subjob-id")
            assert result["status"] == "merged"
            assert result["job_id"] == "subjob-id"
            assert result["pr_number"] == 42


# ===========================================================================
# SUBJOB_CLEANUP constants
# ===========================================================================


class TestSubjobCleanupConstants:
    """Verify the cleanup lists contain expected entries."""

    def test_cleanup_files_contains_workspace(self):
        assert "workspace.md" in orch_main.SUBJOB_CLEANUP_FILES
        assert "plan.md" in orch_main.SUBJOB_CLEANUP_FILES
        assert "todos.yaml" in orch_main.SUBJOB_CLEANUP_FILES

    def test_cleanup_dirs_contains_archive(self):
        assert "archive" in orch_main.SUBJOB_CLEANUP_DIRS
        assert "tools" in orch_main.SUBJOB_CLEANUP_DIRS
        assert "documents" in orch_main.SUBJOB_CLEANUP_DIRS
