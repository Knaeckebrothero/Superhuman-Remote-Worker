"""Tests for ``orchestrator/services/job_provisioning.py``.

Covers the shared Gitea provisioning extracted from the ``POST /api/jobs``
handler so every job-creation path uses it: the three repo/branch branches
(standalone root / project job / project fallback / subjob), the hardened
creator grant (username + full_name + sub), the cloud-baseline seed gate,
the ``is_initialized`` no-op, and — critically — asyncpg UUID coercion
(``job_row`` carries native ``uuid.UUID`` objects that the ``postgres_db``
helpers do ``UUID(arg)`` on and would raise if handed a UUID instance).
DB + Gitea are mocked; no Postgres/Gitea container needed.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.job_provisioning import provision_job_repo


class _AsyncCtx:
    """Minimal ``async with`` returning a fixed value (asyncpg pool/conn shape)."""

    def __init__(self, value):
        self._value = value

    async def __aenter__(self):
        return self._value

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _make_gitea(*, initialized: bool = True) -> MagicMock:
    g = MagicMock()
    g.is_initialized = initialized
    g.create_repo = AsyncMock(
        return_value="http://srw:tok@srw-gitea:3000/srw/job-xxxx.git"
    )
    g.create_branch = AsyncMock(return_value=True)
    g.grant_user_repo_access = AsyncMock(return_value=True)
    return g


def _make_db(
    *,
    user: dict | None = None,
    project: dict | None = None,
    project_repos: list | None = None,
    parent: dict | None = None,
) -> MagicMock:
    conn = MagicMock()
    conn.execute = AsyncMock()
    db = MagicMock()
    db.acquire = MagicMock(return_value=_AsyncCtx(conn))
    db.merge_job_context = AsyncMock()
    db.get_user = AsyncMock(return_value=user)
    db.get_project = AsyncMock(return_value=project)
    db.get_project_repositories = AsyncMock(return_value=project_repos or [])
    db.get_job = AsyncMock(return_value=parent)
    return db


def _creator() -> dict:
    return {
        "email": "max@stud.fra-uas.de",
        "preferred_username": "max",
        "display_name": "Max Mustermann",
        "keycloak_sub": "d0d7dea6-6c7b-4fb2-9d47-b84096253db3",
    }


@pytest.fixture
def patched_seed(monkeypatch):
    """Patch the locally-imported ``fire_baseline_seed`` so the cloud-seed
    branch never schedules a real background task."""
    seed = MagicMock()
    monkeypatch.setattr("services.job_cloud_baseline.fire_baseline_seed", seed)
    return seed


class TestProvisionJobRepo:
    @pytest.mark.asyncio
    async def test_noop_when_gitea_uninitialized(self) -> None:
        g = _make_gitea(initialized=False)
        db = _make_db()
        row = {"id": uuid.uuid4(), "user_id": uuid.uuid4()}

        out = await provision_job_repo(
            job_row=row,
            gitea_client=g,
            postgres_db=db,
            main_cloud_router=MagicMock(),
        )

        assert out is row
        g.create_repo.assert_not_called()
        g.create_branch.assert_not_called()
        db.get_user.assert_not_called()

    @pytest.mark.asyncio
    async def test_standalone_root_creates_repo_and_grants(self, patched_seed) -> None:
        g = _make_gitea()
        db = _make_db(user=_creator())
        jid = uuid.UUID("1793b2a8-94bd-4f3c-bf4a-5ff51c9e012e")
        row = {
            "id": jid,
            "parent_job_id": None,
            "project_id": None,
            "user_id": uuid.UUID("48de2860-107f-4a8c-9a24-3a1e6c352ded"),
            "config_name": "developer",
        }

        await provision_job_repo(
            job_row=row,
            gitea_client=g,
            postgres_db=db,
            main_cloud_router=MagicMock(),
        )

        g.create_repo.assert_awaited_once_with("job-1793b2a8")
        assert row["repo_name"] == "job-1793b2a8"
        db.merge_job_context.assert_awaited_once()
        assert db.merge_job_context.await_args.args[0] == str(jid)
        # Grant hardening: username/full_name/sub are passed (not email-only).
        gk = g.grant_user_repo_access.await_args
        assert gk.args[0] == "max@stud.fra-uas.de"
        assert gk.args[1] == "job-1793b2a8"
        assert gk.kwargs["username"] == "max"
        assert gk.kwargs["full_name"] == "Max Mustermann"
        assert gk.kwargs["sub"] == "d0d7dea6-6c7b-4fb2-9d47-b84096253db3"
        # No project → no cloud-baseline seed.
        patched_seed.assert_not_called()

    @pytest.mark.asyncio
    async def test_uuid_coercion_for_db_helpers(self, patched_seed) -> None:
        """``job_row`` ids are native UUIDs; the postgres_db helpers that do
        ``UUID(arg)`` must receive ``str`` or they raise."""
        g = _make_gitea()
        db = _make_db(user=_creator())
        row = {
            "id": uuid.uuid4(),
            "parent_job_id": None,
            "project_id": None,
            "user_id": uuid.uuid4(),
            "config_name": "developer",
        }

        await provision_job_repo(
            job_row=row,
            gitea_client=g,
            postgres_db=db,
            main_cloud_router=MagicMock(),
        )

        assert isinstance(db.get_user.await_args.args[0], str)
        assert isinstance(db.merge_job_context.await_args.args[0], str)

    @pytest.mark.asyncio
    async def test_project_job_branches_shared_repo(self, patched_seed) -> None:
        g = _make_gitea()
        db = _make_db(
            user=_creator(),
            project_repos=[
                {"name": "project-1a387b4d-jobs", "repo_url": "http://x/y.git"}
            ],
            project={"main_cloud_folder_handle": None},
        )
        jid = uuid.UUID("abcdef12-0000-0000-0000-000000000000")
        row = {
            "id": jid,
            "parent_job_id": None,
            "project_id": uuid.uuid4(),
            "user_id": uuid.uuid4(),
            "config_name": "default",
        }

        await provision_job_repo(
            job_row=row,
            gitea_client=g,
            postgres_db=db,
            main_cloud_router=MagicMock(),
        )

        g.create_branch.assert_awaited_once_with(
            "project-1a387b4d-jobs", "job/abcdef12", from_branch="main"
        )
        assert row["repo_name"] == "project-1a387b4d-jobs"
        assert row["branch_name"] == "job/abcdef12"
        g.create_repo.assert_not_called()
        assert isinstance(db.get_project_repositories.await_args.args[0], str)

    @pytest.mark.asyncio
    async def test_project_job_fallback_per_job_repo(self, patched_seed) -> None:
        """A project with no jobs repo falls back to a per-job repo."""
        g = _make_gitea()
        db = _make_db(
            user=_creator(),
            project_repos=[],
            project={"main_cloud_folder_handle": None},
        )
        jid = uuid.UUID("abcdef12-0000-0000-0000-000000000000")
        row = {
            "id": jid,
            "parent_job_id": None,
            "project_id": uuid.uuid4(),
            "user_id": uuid.uuid4(),
            "config_name": "default",
        }

        await provision_job_repo(
            job_row=row,
            gitea_client=g,
            postgres_db=db,
            main_cloud_router=MagicMock(),
        )

        g.create_repo.assert_awaited_once_with("job-abcdef12")
        assert row["repo_name"] == "job-abcdef12"
        g.create_branch.assert_not_called()

    @pytest.mark.asyncio
    async def test_subjob_branches_on_parent_repo(self, patched_seed) -> None:
        g = _make_gitea()
        parent = {
            "id": uuid.uuid4(),
            "repo_name": "job-deadbeef",
            "branch_name": "main",
            "context": {"git_remote_url": "http://x/job-deadbeef.git"},
            "parent_job_id": None,
            "project_id": None,
        }
        db = _make_db(user=_creator(), parent=parent)
        jid = uuid.UUID("abcdef12-0000-0000-0000-000000000000")
        row = {
            "id": jid,
            "parent_job_id": uuid.uuid4(),
            "project_id": None,
            "user_id": uuid.uuid4(),
            "config_name": "critic",
        }

        await provision_job_repo(
            job_row=row,
            gitea_client=g,
            postgres_db=db,
            main_cloud_router=MagicMock(),
        )

        g.create_branch.assert_awaited_once_with(
            "job-deadbeef", "subjob/abcdef12/critic", from_branch="main"
        )
        assert row["repo_name"] == "job-deadbeef"
        assert row["branch_name"] == "subjob/abcdef12/critic"
        assert isinstance(db.get_job.await_args.args[0], str)

    @pytest.mark.asyncio
    async def test_cloud_seed_fires_when_project_has_folder(self, patched_seed) -> None:
        g = _make_gitea()
        mcr = MagicMock()
        db = _make_db(
            user=_creator(),
            project_repos=[{"name": "p-jobs", "repo_url": "http://x/y.git"}],
            project={"main_cloud_folder_handle": "personal/Projects/X"},
        )
        row = {
            "id": uuid.uuid4(),
            "parent_job_id": None,
            "project_id": uuid.uuid4(),
            "user_id": uuid.uuid4(),
            "config_name": "default",
        }

        await provision_job_repo(
            job_row=row,
            gitea_client=g,
            postgres_db=db,
            main_cloud_router=mcr,
        )

        patched_seed.assert_called_once()
        kwargs = patched_seed.call_args.kwargs
        assert kwargs["repo_name"] == "p-jobs"
        assert kwargs["main_cloud_router"] is mcr

    @pytest.mark.asyncio
    async def test_cloud_seed_skipped_without_folder(self, patched_seed) -> None:
        g = _make_gitea()
        db = _make_db(
            user=_creator(),
            project_repos=[{"name": "p-jobs", "repo_url": "http://x/y.git"}],
            project={"main_cloud_folder_handle": None},
        )
        row = {
            "id": uuid.uuid4(),
            "parent_job_id": None,
            "project_id": uuid.uuid4(),
            "user_id": uuid.uuid4(),
            "config_name": "default",
        }

        await provision_job_repo(
            job_row=row,
            gitea_client=g,
            postgres_db=db,
            main_cloud_router=MagicMock(),
        )

        patched_seed.assert_not_called()

    @pytest.mark.asyncio
    async def test_grant_skipped_when_repo_not_created(self, patched_seed) -> None:
        """If ``create_repo`` returns falsy, no repo_name is set → no grant,
        no crash (mirrors the original best-effort behaviour)."""
        g = _make_gitea()
        g.create_repo = AsyncMock(return_value=None)
        db = _make_db(user=_creator())
        row = {
            "id": uuid.uuid4(),
            "parent_job_id": None,
            "project_id": None,
            "user_id": uuid.uuid4(),
            "config_name": "developer",
        }

        await provision_job_repo(
            job_row=row,
            gitea_client=g,
            postgres_db=db,
            main_cloud_router=MagicMock(),
        )

        assert "repo_name" not in row
        g.grant_user_repo_access.assert_not_called()
