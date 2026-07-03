"""Tests for the loop v2 completion squash-merge + retro writer.

``merge_loop_job_branch`` turns a completed loop job's ``job/<id>`` branch
into ONE squash commit on ``main`` and returns a literal merge status —
the loop's artifact-integrity signal (replaces the v1 SHA-compare no-op
guard). ``write_loop_retro`` records the outcome as a standardized
``retros/NNN-<role>-<jobid8>.md`` on ``main``.

Design: docs/features/loop_repo_compounding_v2.md. Gitea is mocked
(pattern: tests/test_job_provisioning.py).
"""

from __future__ import annotations

import base64
import json
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.project_loops import merge_loop_job_branch, write_loop_retro

JOB_ID = uuid.UUID("abcdef12-3456-7890-abcd-ef1234567890")


def _make_gitea(
    *,
    total_commits: int = 2,
    pr: dict | None = {"number": 7, "url": "http://g/pr/7", "state": "open"},
    merge_ok: bool = True,
) -> MagicMock:
    g = MagicMock()
    g.get_compare = AsyncMock(
        return_value={"total_commits": total_commits, "commits": []}
    )
    g.create_pr = AsyncMock(return_value=pr)
    g.merge_pr = AsyncMock(return_value=merge_ok)
    g.get_branch_head_sha = AsyncMock(return_value="c0ffee00" * 5)
    g.change_files = AsyncMock(return_value=True)
    return g


def _job(**over) -> dict:
    row = {
        "id": JOB_ID,
        "repo_name": "project-1a387b4d-jobs",
        "branch_name": "job/abcdef12",
        "description": "Loop iter 15 · DEVELOPER: implement the chosen action",
        "freeze_data": {"notes": "Shipped AC-11; tests green."},
    }
    row.update(over)
    return row


class TestMergeLoopJobBranch:
    @pytest.mark.asyncio
    async def test_squash_merges_branch_and_reports_merged(self) -> None:
        g = _make_gitea()

        status, sha = await merge_loop_job_branch(g, _job())

        assert status == "merged"
        assert sha == "c0ffee00" * 5
        g.get_compare.assert_awaited_once_with(
            "project-1a387b4d-jobs", "main", "job/abcdef12"
        )
        pr = g.create_pr.await_args
        assert pr.args[0] == "project-1a387b4d-jobs"
        assert pr.kwargs["head"] == "job/abcdef12"
        assert pr.kwargs["base"] == "main"
        assert "Loop iter 15" in pr.kwargs["title"]
        assert str(JOB_ID) in pr.kwargs["body"]  # traceability trailer
        m = g.merge_pr.await_args
        assert m.args[:2] == ("project-1a387b4d-jobs", 7)
        assert m.kwargs["merge_strategy"] == "squash"
        # The branch is the audit log — never deleted on merge.
        assert m.kwargs["delete_branch_after_merge"] is False

    @pytest.mark.asyncio
    async def test_no_commits_reports_empty_without_pr(self) -> None:
        g = _make_gitea(total_commits=0)

        status, sha = await merge_loop_job_branch(g, _job())

        assert status == "empty"
        assert sha is None
        g.create_pr.assert_not_called()
        g.merge_pr.assert_not_called()

    @pytest.mark.asyncio
    async def test_legacy_main_branch_job_is_skipped(self) -> None:
        """A v1-era job (branch_name='main') completing after the v2 deploy:
        its push already landed on main — nothing to merge."""
        g = _make_gitea()

        status, sha = await merge_loop_job_branch(g, _job(branch_name="main"))

        assert status == "skipped"
        assert sha is None
        g.get_compare.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_branch_or_repo_is_skipped(self) -> None:
        g = _make_gitea()
        assert (await merge_loop_job_branch(g, _job(branch_name=None)))[0] == "skipped"
        assert (await merge_loop_job_branch(g, _job(repo_name=None)))[0] == "skipped"

    @pytest.mark.asyncio
    async def test_compare_failure_reports_merge_failed(self) -> None:
        g = _make_gitea()
        g.get_compare = AsyncMock(return_value=None)

        status, _ = await merge_loop_job_branch(g, _job())

        assert status == "merge-failed"

    @pytest.mark.asyncio
    async def test_pr_create_failure_reports_merge_failed(self) -> None:
        g = _make_gitea(pr=None)

        status, _ = await merge_loop_job_branch(g, _job())

        assert status == "merge-failed"
        g.merge_pr.assert_not_called()

    @pytest.mark.asyncio
    async def test_merge_failure_reports_merge_failed(self) -> None:
        g = _make_gitea(merge_ok=False)

        status, _ = await merge_loop_job_branch(g, _job())

        assert status == "merge-failed"


class TestWriteLoopRetro:
    @pytest.mark.asyncio
    async def test_writes_standardized_retro_to_main(self) -> None:
        g = _make_gitea()
        ctx = {"loop_role": "developer", "loop_iteration": 15}

        ok = await write_loop_retro(
            g,
            _job(),
            ctx=ctx,
            merge_status="merged",
            merged_sha="deadbeef" * 5,
        )

        assert ok is True
        cf = g.change_files.await_args
        assert cf.args[0] == "project-1a387b4d-jobs"
        assert cf.args[1] == "main"
        entry = cf.args[2][0]
        assert entry["path"] == "retros/015-developer-abcdef12.md"
        text = base64.b64decode(entry["content_b64"]).decode()
        # OKF-style frontmatter carries the mechanical truth.
        assert "type: retro" in text
        assert "iteration: 15" in text
        assert "role: developer" in text
        assert f"job: {JOB_ID}" in text
        assert "branch: job/abcdef12" in text
        assert "merge_status: merged" in text
        assert ("deadbeef" * 5) in text
        # Body carries the agent's own completion notes.
        assert "Shipped AC-11; tests green." in text
        assert "merged" in cf.kwargs["message"]

    @pytest.mark.asyncio
    async def test_freeze_data_jsonb_string_is_parsed(self) -> None:
        """asyncpg returns JSONB as raw JSON strings — the retro writer must
        not crash or embed the raw JSON blob."""
        g = _make_gitea()
        job = _job(freeze_data=json.dumps({"notes": "String-typed freeze notes."}))

        ok = await write_loop_retro(
            g, job, ctx={"loop_role": "critic", "loop_iteration": 3},
            merge_status="empty", merged_sha=None,
        )

        assert ok is True
        text = base64.b64decode(g.change_files.await_args.args[2][0]["content_b64"]).decode()
        assert "String-typed freeze notes." in text
        assert '{"notes"' not in text

    @pytest.mark.asyncio
    async def test_failed_job_retro_records_failure(self) -> None:
        g = _make_gitea()

        ok = await write_loop_retro(
            g,
            _job(freeze_data=None),
            ctx={"loop_role": "developer", "loop_iteration": 4},
            merge_status="skipped",
            merged_sha=None,
            failed=True,
            error="agent crash-looped",
        )

        assert ok is True
        text = base64.b64decode(g.change_files.await_args.args[2][0]["content_b64"]).decode()
        assert "status: failed" in text
        assert "agent crash-looped" in text
        assert "(none recorded)" in text  # no freeze_data notes

    @pytest.mark.asyncio
    async def test_missing_repo_returns_false(self) -> None:
        g = _make_gitea()
        ok = await write_loop_retro(
            g, _job(repo_name=None), ctx={}, merge_status="merged", merged_sha=None
        )
        assert ok is False
        g.change_files.assert_not_called()

    @pytest.mark.asyncio
    async def test_change_files_failure_returns_false(self) -> None:
        g = _make_gitea()
        g.change_files = AsyncMock(return_value=False)
        ok = await write_loop_retro(
            g, _job(), ctx={"loop_iteration": 1}, merge_status="merged",
            merged_sha=None,
        )
        assert ok is False
