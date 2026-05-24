"""Tests for the per-job repo model (resolve_job_repo, _graft_subjob_output, etc.)."""

import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

# Add orchestrator/ to sys.path so its internal imports resolve
_orch_dir = str(Path(__file__).parent.parent / "orchestrator")
if _orch_dir not in sys.path:
    sys.path.insert(0, _orch_dir)

# main.py requires VECTOR_DB_URL at module level
os.environ.setdefault("VECTOR_DB_URL", "postgresql://test@localhost/test")

import main as orch_main  # noqa: E402

MODULE = "main"


def _stub_request() -> MagicMock:
    """A minimal Request stub for endpoints whose Track A/B gates need one.
    Gates are patched separately to bypass auth — this just lets the
    handler signature line up."""
    req = MagicMock()
    req.headers = {}
    req.cookies = {}
    return req


def _bypass_job_access_gate(job: dict):
    """Patch `require_job_access` to return (admin_caller, job) so the
    destructive owner-or-admin check in delete_job is satisfied without
    standing up the full auth stack."""
    admin = {"id": "00000000-0000-0000-0000-000000000099", "is_admin": True}
    return patch(
        f"{MODULE}.require_job_access",
        AsyncMock(return_value=(admin, job)),
    )


def _bypass_require_internal():
    """Patch `require_internal` to pass through. P4b agent-internal
    endpoints (subjob_merge etc.) gate on this."""
    return patch(f"{MODULE}.require_internal", AsyncMock(return_value=None))


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
            _bypass_job_access_gate(job),
        ):
            mock_db.delete_job = AsyncMock(return_value=True)
            mock_gitea.is_initialized = True
            mock_gitea.delete_repo = AsyncMock()
            mock_gitea.delete_branch = AsyncMock()

            result = await orch_main.delete_job(_stub_request(), "abcd1234-xxxx")

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
            _bypass_job_access_gate(job),
        ):
            mock_db.delete_job = AsyncMock(return_value=True)
            mock_gitea.is_initialized = True
            mock_gitea.delete_branch = AsyncMock()
            mock_gitea.delete_repo = AsyncMock()

            result = await orch_main.delete_job(_stub_request(), "abcd1234-xxxx")

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
            _bypass_job_access_gate(job),
        ):
            mock_db.delete_job = AsyncMock(return_value=True)
            mock_db.get_project_repositories = AsyncMock(
                return_value=[{"name": "project-jobs-repo"}]
            )
            mock_gitea.is_initialized = True
            mock_gitea.delete_branch = AsyncMock()
            mock_gitea.delete_repo = AsyncMock()

            result = await orch_main.delete_job(_stub_request(), "legacy-id")

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
            _bypass_job_access_gate(job),
        ):
            mock_db.delete_job = AsyncMock(return_value=True)
            mock_gitea.is_initialized = False

            result = await orch_main.delete_job(_stub_request(), "some-id")

            assert result == {"status": "deleted"}
            mock_gitea.delete_repo.assert_not_called()
            mock_gitea.delete_branch.assert_not_called()

    @pytest.mark.asyncio
    async def test_job_not_found_raises_404(self):
        # The gate (`require_job_access`) is what raises 404 for missing
        # jobs now — let the real helper run with a patched db that returns None.
        with (
            patch(f"{MODULE}.postgres_db") as mock_db,
            patch(
                "security.access.require_approved_user",
                AsyncMock(
                    return_value={
                        "id": "00000000-0000-0000-0000-000000000099",
                        "is_admin": True,
                    }
                ),
            ),
        ):
            mock_db.get_job = AsyncMock(return_value=None)

            with pytest.raises(HTTPException) as exc_info:
                await orch_main.delete_job(_stub_request(), "missing-id")
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

        with (
            patch(f"{MODULE}.postgres_db") as mock_db,
            _bypass_require_internal(),
        ):
            mock_db.get_job = AsyncMock(return_value=job)

            with pytest.raises(HTTPException) as exc_info:
                await orch_main.subjob_merge(_stub_request(), "root-job-id")
            assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_returns_404_for_missing_job(self):
        with (
            patch(f"{MODULE}.postgres_db") as mock_db,
            _bypass_require_internal(),
        ):
            mock_db.get_job = AsyncMock(return_value=None)

            with pytest.raises(HTTPException) as exc_info:
                await orch_main.subjob_merge(_stub_request(), "missing-id")
            assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_returns_skipped_when_no_branch(self):
        """Subjob without branch config returns skipped."""
        job = {"parent_job_id": "parent-id", "branch_name": None, "repo_name": None}

        with (
            patch(f"{MODULE}.postgres_db") as mock_db,
            patch(f"{MODULE}.gitea_client") as mock_gitea,
            _bypass_require_internal(),
        ):
            mock_db.get_job = AsyncMock(return_value=job)
            mock_gitea.is_initialized = True

            result = await orch_main.subjob_merge(_stub_request(), "subjob-id")
            assert result["status"] == "skipped"

    @pytest.mark.asyncio
    async def test_returns_graft_result(self):
        """Happy path: returns graft result from _graft_subjob_output."""
        job = {
            "parent_job_id": "parent-id",
            "branch_name": "subjob/abc/scholar",
            "repo_name": "job-parent",
        }

        with (
            patch(f"{MODULE}.postgres_db") as mock_db,
            patch(f"{MODULE}._graft_subjob_output", new_callable=AsyncMock) as mock_graft,
            _bypass_require_internal(),
        ):
            mock_db.get_job = AsyncMock(return_value=job)
            mock_graft.return_value = {"status": "grafted", "output_path": "outputs/001-scholar-abc"}

            result = await orch_main.subjob_merge(_stub_request(), "subjob-id")
            assert result["status"] == "grafted"
            assert result["job_id"] == "subjob-id"
            assert result["output_path"] == "outputs/001-scholar-abc"


# ===========================================================================
# _next_output_ordinal
# ===========================================================================


class _OutputsFake:
    """Minimal gitea fake exposing list_contents for an `outputs/` dir."""

    def __init__(self, outputs_dirs: list[str]):
        # outputs_dirs: directory names directly under outputs/, e.g. ["001-scholar-aa"]
        self._dirs = outputs_dirs
        self.is_initialized = True

    async def list_contents(self, repo, path="", ref=None):
        if path != "outputs":
            return []
        return [{"name": d, "path": f"outputs/{d}", "type": "dir"} for d in self._dirs]


class TestNextOutputOrdinal:
    @pytest.mark.asyncio
    async def test_first_ordinal_is_001(self):
        fake = _OutputsFake([])
        with patch(f"{MODULE}.gitea_client", fake):
            assert await orch_main._next_output_ordinal("job-x", "main") == "001"

    @pytest.mark.asyncio
    async def test_increments_past_highest(self):
        fake = _OutputsFake(["001-scholar-aa", "002-critic-bb", "010-developer-cc"])
        with patch(f"{MODULE}.gitea_client", fake):
            assert await orch_main._next_output_ordinal("job-x", "main") == "011"

    @pytest.mark.asyncio
    async def test_ignores_non_numbered_entries(self):
        fake = _OutputsFake(["notes", "003-scholar-dd"])
        with patch(f"{MODULE}.gitea_client", fake):
            assert await orch_main._next_output_ordinal("job-x", "main") == "004"


import base64 as _b64

# ===========================================================================
# _graft_subjob_output
# ===========================================================================


class _GraftFakeGitea:
    """Models per-branch trees as {branch: {path: bytes}} and the graft I/O.

    - list_tree(ref) -> [{path, type:"blob"}] for that branch
    - get_file_bytes(path, ref) -> bytes
    - list_contents("outputs", ref) -> dir entries under outputs/ on that branch
    - change_files(branch, files) -> add files (base64) to that branch's tree
    """

    def __init__(self, trees: dict[str, dict[str, bytes]]):
        self.trees = {b: dict(t) for b, t in trees.items()}
        self.is_initialized = True

    async def list_tree(self, repo, ref):
        return [{"path": p, "type": "blob"} for p in self.trees.get(ref, {})]

    async def get_file_bytes(self, repo, file_path, ref=None):
        return self.trees.get(ref, {}).get(file_path)

    async def list_contents(self, repo, path="", ref=None):
        if path != "outputs":
            return []
        names = set()
        for p in self.trees.get(ref, {}):
            if p.startswith("outputs/"):
                names.add(p.split("/")[1])
        return [{"name": n, "path": f"outputs/{n}", "type": "dir"} for n in names]

    async def change_files(self, repo, branch, files, message):
        tree = self.trees.setdefault(branch, {})
        for f in files:
            tree[f["path"]] = _b64.b64decode(f["content_b64"])
        return True


def _subjob(**over):
    base = {
        "id": "sub-uuid-1234abcd",
        "parent_job_id": "parent-uuid",
        "branch_name": "subjob/1234abcd/scholar",
        "repo_name": "job-parent12",
        "config_name": "scholar",
        "description": "research",
        "context": {},
    }
    base.update(over)
    return base


class TestGraftSubjobOutput:
    @pytest.mark.asyncio
    async def test_grafts_output_to_namespaced_dir_and_leaves_parent_untouched(self):
        fake = _GraftFakeGitea(
            {
                "main": {"documents/corpus.pdf": b"PARENT", "src/app.py": b"code"},
                "subjob/1234abcd/scholar": {
                    "documents/corpus.pdf": b"PARENT",   # inherited from fork
                    "src/app.py": b"code",
                    "output/ideas/idea.md": b"# idea",
                    "output/report.pdf": b"\x89PDFbytes",
                    "workspace.md": b"scratch",          # NOT under output/
                },
            }
        )
        with (
            patch(f"{MODULE}.postgres_db") as db,
            patch(f"{MODULE}.gitea_client", fake),
        ):
            db.get_job = AsyncMock(
                side_effect=lambda j: {"sub-uuid-1234abcd": _subjob(), "parent-uuid": {"branch_name": None}}.get(j)
            )
            db.update_job_merge_status = AsyncMock()
            db.update_job_context = AsyncMock()

            result = await orch_main._graft_subjob_output("sub-uuid-1234abcd")

        assert result["status"] == "grafted"
        assert result["output_path"] == "outputs/001-scholar-sub-uuid"
        # output/ contents relocated under the namespaced dir, prefix stripped:
        assert fake.trees["main"]["outputs/001-scholar-sub-uuid/ideas/idea.md"] == b"# idea"
        assert fake.trees["main"]["outputs/001-scholar-sub-uuid/report.pdf"] == b"\x89PDFbytes"
        # parent content untouched; scratch + inherited tree NOT propagated:
        assert fake.trees["main"]["documents/corpus.pdf"] == b"PARENT"
        assert "outputs/001-scholar-sub-uuid/workspace.md" not in fake.trees["main"]
        db.update_job_merge_status.assert_awaited_with("sub-uuid-1234abcd", merge_status="grafted")

    @pytest.mark.asyncio
    async def test_critic_grafts_nothing(self):
        fake = _GraftFakeGitea(
            {
                "main": {"src/app.py": b"code"},
                "subjob/1234abcd/critic": {"output/reviews/r.md": b"review"},
            }
        )
        critic = _subjob(
            config_name="critic",
            branch_name="subjob/1234abcd/critic",
            context={"verification_target": "parent-uuid"},
        )
        with (
            patch(f"{MODULE}.postgres_db") as db,
            patch(f"{MODULE}.gitea_client", fake),
        ):
            db.get_job = AsyncMock(
                side_effect=lambda j: {"sub-uuid-1234abcd": critic, "parent-uuid": {"branch_name": None}}.get(j)
            )
            db.update_job_merge_status = AsyncMock()
            db.update_job_context = AsyncMock()

            result = await orch_main._graft_subjob_output("sub-uuid-1234abcd")

        assert result == {"status": "skipped", "reason": "critic-not-merged"}
        assert all(not k.startswith("outputs/") for k in fake.trees["main"])

    @pytest.mark.asyncio
    async def test_no_output_skipped(self):
        fake = _GraftFakeGitea(
            {"main": {}, "subjob/1234abcd/scholar": {"workspace.md": b"scratch"}}
        )
        with (
            patch(f"{MODULE}.postgres_db") as db,
            patch(f"{MODULE}.gitea_client", fake),
        ):
            db.get_job = AsyncMock(
                side_effect=lambda j: {"sub-uuid-1234abcd": _subjob(), "parent-uuid": {"branch_name": None}}.get(j)
            )
            db.update_job_merge_status = AsyncMock()
            db.update_job_context = AsyncMock()

            result = await orch_main._graft_subjob_output("sub-uuid-1234abcd")

        assert result == {"status": "skipped", "reason": "no-output"}

    @pytest.mark.asyncio
    async def test_ordinal_increments_when_outputs_exist(self):
        fake = _GraftFakeGitea(
            {
                "main": {"outputs/001-scholar-old/x.md": b"old"},
                "subjob/1234abcd/scholar": {"output/y.md": b"new"},
            }
        )
        with (
            patch(f"{MODULE}.postgres_db") as db,
            patch(f"{MODULE}.gitea_client", fake),
        ):
            db.get_job = AsyncMock(
                side_effect=lambda j: {"sub-uuid-1234abcd": _subjob(), "parent-uuid": {"branch_name": None}}.get(j)
            )
            db.update_job_merge_status = AsyncMock()
            db.update_job_context = AsyncMock()

            result = await orch_main._graft_subjob_output("sub-uuid-1234abcd")

        assert result["output_path"] == "outputs/002-scholar-sub-uuid"
        assert fake.trees["main"]["outputs/002-scholar-sub-uuid/y.md"] == b"new"


class TestCompletionGraftWiring:
    @pytest.mark.asyncio
    async def test_graft_fires_for_delegation_child(self):
        # A delegation child has creation_order set; the old gate skipped it.
        called = {}

        async def fake_graft(job_id):
            called["job_id"] = job_id
            return {"status": "grafted", "output_path": "outputs/001-developer-deadbeef"}

        child = {
            "id": "deadbeef-child", "parent_job_id": "p", "creation_order": 0,
            "branch_name": "subjob/deadbeef/developer", "repo_name": "job-p",
            "config_name": "developer",
        }
        with patch(f"{MODULE}._graft_subjob_output", side_effect=fake_graft):
            res = await orch_main._maybe_graft_completed_subjob(child)
        assert called["job_id"] == "deadbeef-child"
        assert res["status"] == "grafted"

    @pytest.mark.asyncio
    async def test_no_graft_for_root_job(self):
        with patch(f"{MODULE}._graft_subjob_output", new_callable=AsyncMock) as g:
            res = await orch_main._maybe_graft_completed_subjob({"id": "r", "parent_job_id": None})
        assert res is None
        g.assert_not_called()


class TestScholarOutputPointer:
    @pytest.mark.asyncio
    async def test_scholar_completion_sets_parent_output_dir_to_graft_path(self):
        scholar = {
            "id": "sch-1", "parent_job_id": "par-1", "status": "completed",
            "context": {"scholar_target": "par-1", "graft_output_path": "outputs/003-scholar-sch1"},
        }
        parent = {"id": "par-1", "status": "waiting", "context": {}}
        captured = {}

        async def upd_ctx(jid, ctx):
            captured[jid] = ctx

        with patch(f"{MODULE}.postgres_db") as db:
            db.get_job = AsyncMock(
                side_effect=lambda j: {"sch-1": scholar, "par-1": parent}.get(j)
            )
            db.update_job_context = AsyncMock(side_effect=upd_ctx)
            db.update_job_status = AsyncMock()
            with patch(f"{MODULE}._trigger_dispatch"):
                await orch_main._handle_scholar_completion(scholar, [])

        assert captured["par-1"]["scholar_output_dir"] == "outputs/003-scholar-sch1"
        assert captured["par-1"]["scholar_completed"] is True
