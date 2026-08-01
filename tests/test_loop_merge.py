"""Tests for the loop v2 completion squash-merge + retro writer.

``merge_loop_job_branch`` turns a completed loop job's ``job/<id>`` branch
into ONE squash commit on ``main`` and returns a literal merge status —
the loop's artifact-integrity signal (replaces the v1 SHA-compare no-op
guard). ``write_loop_retro`` records the outcome as a standardized
``retros/NNN-<role>-<jobid8>.md`` on ``main``.

``merge_loop_job_contribution`` is the §6.4 dispatcher on top
(docs/features/workspace_and_change_records.md): a job whose
``required_deliverables`` name at least one FILE gets a curated merge —
only the contracted files land on ``main`` (one commit), the audit PR is
closed UNMERGED — while every other job takes the ``merge_loop_job_branch``
path byte-identically.

Design: docs/features/loop_repo_compounding_v2.md. Gitea is mocked
(pattern: tests/test_job_provisioning.py).
"""

from __future__ import annotations

import base64
import json
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.project_loops import (
    merge_loop_job_branch,
    merge_loop_job_contribution,
    write_loop_retro,
)

JOB_ID = uuid.UUID("abcdef12-3456-7890-abcd-ef1234567890")


def _tree(paths) -> list[dict]:
    return [{"path": p, "type": "blob", "sha": "beef" * 10} for p in paths]


def _make_gitea(
    *,
    total_commits: int = 2,
    pr: dict | None = {"number": 7, "url": "http://g/pr/7", "state": "open"},
    merge_ok: bool = True,
    branch_files: dict | None = None,
    main_files: list | None = None,
    branch_tree_ok: bool = True,
    main_tree_ok: bool = True,
    change_ok: bool = True,
    close_ok: bool = True,
) -> MagicMock:
    """Mocked gitea surface.

    ``branch_files`` maps branch-tree paths → blob bytes (``None`` value =
    listed in the tree but unreadable, the mid-curation failure case);
    ``main_files`` lists blob paths existing on ``main`` (drives the curated
    commit's create-vs-update choice).
    """
    branch_files = branch_files or {}
    main_files = main_files or []

    g = MagicMock()
    g.get_compare = AsyncMock(
        return_value={"total_commits": total_commits, "commits": []}
    )
    g.create_pr = AsyncMock(return_value=pr)
    g.merge_pr = AsyncMock(return_value=merge_ok)
    g.get_branch_head_sha = AsyncMock(return_value="c0ffee00" * 5)
    g.change_files = AsyncMock(return_value=change_ok)
    g.close_pr = AsyncMock(return_value=close_ok)
    g.comment_on_pr = AsyncMock(return_value=True)

    async def _list_tree(repo: str, ref: str):
        if ref == "main":
            return _tree(main_files) if main_tree_ok else None
        return _tree(list(branch_files)) if branch_tree_ok else None

    async def _get_file_bytes(repo: str, path: str, ref=None):
        return branch_files.get(path)

    g.list_tree = AsyncMock(side_effect=_list_tree)
    g.get_file_bytes = AsyncMock(side_effect=_get_file_bytes)
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


CTX = {"loop_role": "developer", "loop_iteration": 15}


def _contract_job(*deliverables: str, **over) -> dict:
    return _job(context={"required_deliverables": list(deliverables)}, **over)


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
            g,
            job,
            ctx={"loop_role": "critic", "loop_iteration": 3},
            merge_status="empty",
            merged_sha=None,
        )

        assert ok is True
        text = base64.b64decode(
            g.change_files.await_args.args[2][0]["content_b64"]
        ).decode()
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
        text = base64.b64decode(
            g.change_files.await_args.args[2][0]["content_b64"]
        ).decode()
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
            g,
            _job(),
            ctx={"loop_iteration": 1},
            merge_status="merged",
            merged_sha=None,
        )
        assert ok is False

    @pytest.mark.asyncio
    async def test_merge_notes_render_as_their_own_section(self) -> None:
        """§6.4: curated-merge observations (fallback warnings, missing
        contracted paths) are ORCHESTRATOR notes — their own section, never
        mixed into the agent's self-report. ``merge_status`` passes through
        as given: ``curated`` renders like any other status."""
        g = _make_gitea()

        ok = await write_loop_retro(
            g,
            _job(),
            ctx=CTX,
            merge_status="curated",
            merged_sha="deadbeef" * 5,
            merge_notes=[
                "curated merge: 1/2 contracted deliverable(s) copied to main @ deadbeef",
                "contracted deliverable NOT on the branch (not curated): docs/spec.md",
            ],
        )

        assert ok is True
        text = base64.b64decode(
            g.change_files.await_args.args[2][0]["content_b64"]
        ).decode()
        assert "merge_status: curated" in text
        assert (
            "## Merge notes\n\n"
            "- curated merge: 1/2 contracted deliverable(s) copied to main @ deadbeef\n"
            "- contracted deliverable NOT on the branch (not curated): docs/spec.md"
        ) in text
        # The agent's own notes stay where they always were.
        assert "Shipped AC-11; tests green." in text

    @pytest.mark.asyncio
    async def test_no_merge_notes_keeps_legacy_shape(self) -> None:
        g = _make_gitea()
        await write_loop_retro(
            g, _job(), ctx=CTX, merge_status="merged", merged_sha=None
        )
        text = base64.b64decode(
            g.change_files.await_args.args[2][0]["content_b64"]
        ).decode()
        assert "## Merge notes" not in text


class TestMergeLoopJobContribution:
    """The §6.4 curated-merge dispatcher.

    Contract present (≥1 file deliverable) → curated; absent or ``kb:``-only
    → the full squash-merge, provably byte-identical to
    ``merge_loop_job_branch``.
    """

    # -- the curated happy path ------------------------------------------

    @pytest.mark.asyncio
    async def test_curates_contracted_files_only_and_closes_pr(self) -> None:
        g = _make_gitea(
            branch_files={
                "output/report.md": b"# report",
                "src/fix.py": b"print('fixed')\n",
                ".venv/lib/junk.py": b"scratch",
                "notes.txt": b"scratch too",
            },
            # report.md already exists on main → update; fix.py is new → create.
            main_files=["output/report.md", "retros/001-x.md"],
        )
        job = _contract_job("output/report.md", "src/fix.py")

        status, sha, notes = await merge_loop_job_contribution(g, job, ctx=CTX)

        assert status == "curated"
        assert sha == "c0ffee00" * 5
        # ONE commit on main carrying exactly the contracted files.
        g.change_files.assert_awaited_once()
        cf = g.change_files.await_args
        assert cf.args[0] == "project-1a387b4d-jobs"
        assert cf.args[1] == "main"
        files = {f["path"]: f for f in cf.args[2]}
        assert set(files) == {"output/report.md", "src/fix.py"}
        assert files["output/report.md"]["operation"] == "update"
        assert files["src/fix.py"]["operation"] == "create"
        assert base64.b64decode(files["output/report.md"]["content_b64"]) == b"# report"
        assert cf.kwargs["message"] == (
            "curated merge: developer (abcdef12) — 2 deliverable(s)"
        )
        # The PR is the audit trail: created, commented, closed — NEVER merged.
        g.merge_pr.assert_not_called()
        g.close_pr.assert_awaited_once_with("project-1a387b4d-jobs", 7)
        comment = g.comment_on_pr.await_args
        assert comment.args[:2] == ("project-1a387b4d-jobs", 7)
        assert "retros/015-developer-abcdef12.md" in comment.args[2]
        assert ("c0ffee00" * 5) in comment.args[2]
        assert any("curated merge: 2/2" in n for n in notes)

    @pytest.mark.asyncio
    async def test_variant_resolution_repo_prefix_and_canonical(self) -> None:
        """F14 both-spellings rule: the branch may hold a deliverable at
        ``repo/<path>`` or ``<path>`` — first existing variant (canonical
        first) wins, the blob is written back to the SAME path, and the
        spelling is recorded. The manifest itself normalizes (``./repo/``
        prefixes stripped) and survives the JSONB-string context form."""
        g = _make_gitea(
            branch_files={
                "repo/output/report.md": b"prefixed",
                "docs/x.md": b"canonical wins",
                "repo/docs/x.md": b"shadowed",
            },
        )
        job = _job(
            context=json.dumps(
                {"required_deliverables": ["./repo/output/report.md", "docs/x.md"]}
            )
        )

        status, _, notes = await merge_loop_job_contribution(g, job, ctx=CTX)

        assert status == "curated"
        files = {f["path"]: f for f in g.change_files.await_args.args[2]}
        assert set(files) == {"repo/output/report.md", "docs/x.md"}
        assert base64.b64decode(files["docs/x.md"]["content_b64"]) == b"canonical wins"
        assert any(
            "merged output/report.md (branch spelling: repo/output/report.md)" == n
            for n in notes
        )

    @pytest.mark.asyncio
    async def test_partial_curation_lists_missing_paths(self) -> None:
        g = _make_gitea(branch_files={"output/report.md": b"# report"})
        job = _contract_job("output/report.md", "docs/spec.md")

        status, _, notes = await merge_loop_job_contribution(g, job, ctx=CTX)

        assert status == "curated"
        files = [f["path"] for f in g.change_files.await_args.args[2]]
        assert files == ["output/report.md"]
        assert g.change_files.await_args.kwargs["message"] == (
            "curated merge: developer (abcdef12) — 1 deliverable(s)"
        )
        assert any("curated merge: 1/2" in n for n in notes)
        assert any(
            "contracted deliverable NOT on the branch (not curated): docs/spec.md" == n
            for n in notes
        )

    @pytest.mark.asyncio
    async def test_pr_ceremony_failure_never_flips_a_landed_curation(self) -> None:
        """After the curated commit is on main there is no fallback — a full
        merge would re-land the whole branch on top. A failed audit-PR
        creation demotes to a note, nothing more."""
        g = _make_gitea(branch_files={"output/report.md": b"r"}, pr=None)
        job = _contract_job("output/report.md")

        status, sha, notes = await merge_loop_job_contribution(g, job, ctx=CTX)

        assert status == "curated"
        assert sha == "c0ffee00" * 5
        g.merge_pr.assert_not_called()
        assert any("audit PR could not be created" in n for n in notes)

    # -- structural guards keep the full-merge vocabulary -----------------

    @pytest.mark.asyncio
    async def test_contract_job_with_empty_branch_reports_empty(self) -> None:
        g = _make_gitea(total_commits=0)
        result = await merge_loop_job_contribution(
            g, _contract_job("output/report.md"), ctx=CTX
        )
        assert result == ("empty", None, [])
        g.list_tree.assert_not_called()

    @pytest.mark.asyncio
    async def test_contract_job_without_branch_is_skipped(self) -> None:
        g = _make_gitea()
        result = await merge_loop_job_contribution(
            g, _contract_job("output/report.md", branch_name=None), ctx=CTX
        )
        assert result == ("skipped", None, [])

    # -- fallbacks: never lose work to a curation bug ---------------------

    @pytest.mark.asyncio
    async def test_none_of_the_contracted_files_falls_back_to_full_merge(
        self,
    ) -> None:
        """An empty curation would silently discard whatever work exists —
        the deliverable gate polices missing deliverables, not the merge."""
        g = _make_gitea(branch_files={"scratch.txt": b"only scratch"})
        job = _contract_job("output/report.md", "docs/spec.md")

        status, sha, notes = await merge_loop_job_contribution(g, job, ctx=CTX)

        assert status == "merged"
        assert sha == "c0ffee00" * 5
        g.change_files.assert_not_called()  # no curated commit
        m = g.merge_pr.await_args
        assert m.kwargs["merge_strategy"] == "squash"
        assert m.kwargs["delete_branch_after_merge"] is False
        assert len(notes) == 1
        assert "FELL BACK to full squash-merge" in notes[0]
        assert "none of the 2 contracted file deliverable(s)" in notes[0]

    @pytest.mark.asyncio
    async def test_unreadable_blob_falls_back_to_full_merge(self) -> None:
        g = _make_gitea(branch_files={"output/report.md": None})  # in tree, no bytes
        job = _contract_job("output/report.md")

        status, _, notes = await merge_loop_job_contribution(g, job, ctx=CTX)

        assert status == "merged"
        g.change_files.assert_not_called()
        g.merge_pr.assert_awaited_once()
        assert any("blob unreadable" in n for n in notes)

    @pytest.mark.asyncio
    async def test_refused_curated_commit_falls_back_to_full_merge(self) -> None:
        g = _make_gitea(branch_files={"output/report.md": b"r"}, change_ok=False)
        job = _contract_job("output/report.md")

        status, _, notes = await merge_loop_job_contribution(g, job, ctx=CTX)

        assert status == "merged"
        g.merge_pr.assert_awaited_once()
        assert any("curated commit refused" in n for n in notes)

    @pytest.mark.asyncio
    async def test_unreadable_main_tree_falls_back_to_full_merge(self) -> None:
        g = _make_gitea(branch_files={"output/report.md": b"r"}, main_tree_ok=False)
        job = _contract_job("output/report.md")

        status, _, notes = await merge_loop_job_contribution(g, job, ctx=CTX)

        assert status == "merged"
        g.change_files.assert_not_called()
        assert any("main tree unreadable" in n for n in notes)

    @pytest.mark.asyncio
    async def test_unexpected_curation_exception_falls_back(self) -> None:
        g = _make_gitea(branch_files={"output/report.md": b"r"})
        g.list_tree = AsyncMock(side_effect=RuntimeError("gitea hiccup"))
        job = _contract_job("output/report.md")

        status, _, notes = await merge_loop_job_contribution(g, job, ctx=CTX)

        assert status == "merged"
        g.merge_pr.assert_awaited_once()
        assert any("FELL BACK" in n for n in notes)

    # -- no contract / kb:-only: byte-identical full merge ----------------

    @pytest.mark.asyncio
    async def test_no_contract_is_call_for_call_identical_to_full_merge(
        self,
    ) -> None:
        """Provable pass-through: the dispatcher with no contract drives the
        mocked client through EXACTLY the calls ``merge_loop_job_branch``
        makes — same methods, same args, same order."""
        g_direct, g_dispatch = _make_gitea(), _make_gitea()

        direct = await merge_loop_job_branch(g_direct, _job())
        status, sha, notes = await merge_loop_job_contribution(
            g_dispatch, _job(), ctx=CTX
        )

        assert (status, sha) == direct == ("merged", "c0ffee00" * 5)
        assert notes == []
        assert g_dispatch.mock_calls == g_direct.mock_calls
        g_dispatch.list_tree.assert_not_called()
        g_dispatch.close_pr.assert_not_called()

    @pytest.mark.asyncio
    async def test_kb_only_contract_takes_the_full_merge(self) -> None:
        """``kb:`` deliverables are store-backed, never files — a contract of
        only those curates nothing and must not change the merge."""
        g_direct, g_dispatch = _make_gitea(), _make_gitea()
        kb_job = _contract_job("kb:iteration-findings", "kb:next-actions")

        direct = await merge_loop_job_branch(g_direct, _job())
        status, sha, notes = await merge_loop_job_contribution(
            g_dispatch, kb_job, ctx=CTX
        )

        assert (status, sha) == direct == ("merged", "c0ffee00" * 5)
        assert notes == []
        assert g_dispatch.mock_calls == g_direct.mock_calls
        g_dispatch.change_files.assert_not_called()
