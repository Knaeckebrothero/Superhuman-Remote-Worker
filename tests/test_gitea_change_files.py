"""Unit tests for GiteaClient.change_files and get_commits."""

import os
from unittest.mock import AsyncMock, MagicMock

import pytest

os.environ.setdefault("VECTOR_DB_URL", "postgresql://test@localhost/test")

from orchestrator.services import gitea as gitea_mod  # noqa: E402


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "misleading_body", "expected"),
    [
        (204, '{"message":"pull request has not been merged","status":404}', True),
        (404, '{"merged":true,"message":"already merged"}', False),
        (405, '{"merged":true}', None),
    ],
)
async def test_probe_pr_merged_uses_status_code_never_body(
    status_code, misleading_body, expected
):
    gc = gitea_mod.GiteaClient.__new__(gitea_mod.GiteaClient)
    gc._initialized = True
    gc._url = "http://gitea"
    gc._user = "srw"

    resp = MagicMock(status_code=status_code, text=misleading_body)
    client = MagicMock()
    client.get = AsyncMock(return_value=resp)
    gc._get_client = MagicMock(return_value=client)

    assert await gc.probe_pr_merged("project-jobs", 41) is expected
    client.get.assert_awaited_once_with(
        "http://gitea/api/v1/repos/srw/project-jobs/pulls/41/merge"
    )


@pytest.mark.asyncio
async def test_list_pull_requests_requests_all_states_and_normalizes_refs():
    gc = gitea_mod.GiteaClient.__new__(gitea_mod.GiteaClient)
    gc._initialized = True
    gc._url = "http://gitea"
    gc._user = "srw"

    resp = MagicMock(status_code=200)
    resp.json.return_value = [
        {
            "number": 12,
            "title": "terminal marker",
            "body": "trailers",
            "state": "closed",
            "head": {"ref": "job/abcdef12", "sha": "a" * 40},
            "base": {"ref": "main", "sha": "b" * 40},
        }
    ]
    client = MagicMock()
    client.get = AsyncMock(return_value=resp)
    gc._get_client = MagicMock(return_value=client)

    pulls = await gc.list_pull_requests("project-jobs", state="all", page=3, limit=50)

    client.get.assert_awaited_once_with(
        "http://gitea/api/v1/repos/srw/project-jobs/pulls",
        params={"state": "all", "page": 3, "limit": 50},
    )
    assert pulls == [
        {
            "number": 12,
            "title": "terminal marker",
            "body": "trailers",
            "state": "closed",
            "head": "job/abcdef12",
            "base": "main",
        }
    ]


@pytest.mark.asyncio
async def test_change_files_posts_batch_create_payload():
    gc = gitea_mod.GiteaClient.__new__(gitea_mod.GiteaClient)  # bypass __init__
    gc._initialized = True
    gc._url = "http://gitea"
    gc._user = "srw"

    resp = MagicMock()
    resp.status_code = 201
    client = MagicMock()
    client.post = AsyncMock(return_value=resp)
    gc._get_client = MagicMock(return_value=client)

    ok = await gc.change_files(
        "job-parent12",
        "main",
        [
            {"path": "outputs/001-scholar-abcd1234/a.md", "content_b64": "YQ=="},
            {"path": "outputs/001-scholar-abcd1234/b.bin", "content_b64": "Yg=="},
        ],
        message="Graft outputs/001-scholar-abcd1234",
    )

    assert ok is True
    client.post.assert_awaited_once()
    url = client.post.await_args.args[0]
    body = client.post.await_args.kwargs["json"]
    assert url == "http://gitea/api/v1/repos/srw/job-parent12/contents"
    assert body["branch"] == "main"
    assert body["message"] == "Graft outputs/001-scholar-abcd1234"
    assert body["files"] == [
        {
            "operation": "create",
            "path": "outputs/001-scholar-abcd1234/a.md",
            "content": "YQ==",
        },
        {
            "operation": "create",
            "path": "outputs/001-scholar-abcd1234/b.bin",
            "content": "Yg==",
        },
    ]


@pytest.mark.asyncio
async def test_get_commits_uses_repo_commits_endpoint():
    """Regression: `/git/commits` only resolves commit SHAs and 404s on branch
    names (silently returning None), which made the loop no-op guard skip
    every check. The listing endpoint is `/commits`, which accepts branches.
    """
    gc = gitea_mod.GiteaClient.__new__(gitea_mod.GiteaClient)  # bypass __init__
    gc._initialized = True
    gc._url = "http://gitea"
    gc._user = "srw"

    resp = MagicMock()
    resp.status_code = 200
    resp.json = MagicMock(
        return_value=[
            {
                "sha": "abc123",
                "commit": {
                    "message": "feat: thing",
                    "author": {"name": "dev", "date": "2026-07-03T00:00:00Z"},
                },
            }
        ]
    )
    client = MagicMock()
    client.get = AsyncMock(return_value=resp)
    gc._get_client = MagicMock(return_value=client)

    commits = await gc.get_commits("job-parent12", sha="main", limit=1)

    url = client.get.await_args.args[0]
    params = client.get.await_args.kwargs["params"]
    assert url == "http://gitea/api/v1/repos/srw/job-parent12/commits"
    assert "git/commits" not in url
    assert params["sha"] == "main"
    assert commits == [
        {
            "sha": "abc123",
            "message": "feat: thing",
            "author": "dev",
            "date": "2026-07-03T00:00:00Z",
        }
    ]


def _client_with_history(commits_newest_first, page_limit=50):
    """A GiteaClient whose get_commits pages over the given history."""
    gc = gitea_mod.GiteaClient.__new__(gitea_mod.GiteaClient)  # bypass __init__
    gc._initialized = True
    gc._url = "http://gitea"
    gc._user = "srw"

    async def fake_get_commits(repo_name, sha="main", page=1, limit=20):
        start = (page - 1) * limit
        return commits_newest_first[start : start + limit]

    gc.get_commits = AsyncMock(side_effect=fake_get_commits)
    gc.get_branch_head_sha = AsyncMock(return_value=None)
    return gc


def _commit(n):
    return {
        "sha": f"{n:02d}" + "e" * 38,
        "message": f"c{n}",
        "author": "dev",
        "date": "2026-07-30T00:00:00Z",
    }


@pytest.mark.asyncio
async def test_commits_between_cuts_at_since_ref_exclusive():
    history = [_commit(3), _commit(2), _commit(1)]
    gc = _client_with_history(history)

    result = await gc.get_commits_between("repo", history[2]["sha"], "main")

    assert result == {
        "total_commits": 2,
        "commits": [history[0], history[1]],
    }
    gc.get_branch_head_sha.assert_not_called()  # hex ref needs no resolution


@pytest.mark.asyncio
async def test_commits_between_matches_short_sha_prefix():
    history = [_commit(3), _commit(2), _commit(1)]
    gc = _client_with_history(history)

    result = await gc.get_commits_between("repo", history[1]["sha"][:8], "main")

    assert [c["sha"] for c in result["commits"]] == [history[0]["sha"]]


@pytest.mark.asyncio
async def test_commits_between_paginates_past_first_page():
    history = [_commit(n) for n in range(60, 0, -1)]  # 60 commits, newest first
    gc = _client_with_history(history)

    # since_ref is the 55th-newest commit -> 54 commits collected across 2 pages
    result = await gc.get_commits_between("repo", history[54]["sha"], "main")

    assert result["total_commits"] == 54
    assert "truncated" not in result
    assert gc.get_commits.await_count == 2


@pytest.mark.asyncio
async def test_commits_between_resolves_branch_base_via_head_sha():
    history = [_commit(3), _commit(2), _commit(1)]
    gc = _client_with_history(history)
    gc.get_branch_head_sha = AsyncMock(return_value=history[1]["sha"])

    result = await gc.get_commits_between("repo", "release/v1", "main")

    gc.get_branch_head_sha.assert_awaited_once_with("repo", "release/v1")
    assert [c["sha"] for c in result["commits"]] == [history[0]["sha"]]


@pytest.mark.asyncio
async def test_commits_between_unfound_base_returns_all_truncated():
    history = [_commit(3), _commit(2), _commit(1)]
    gc = _client_with_history(history)

    result = await gc.get_commits_between("repo", "f" * 40, "main")

    assert result["truncated"] is True
    assert result["total_commits"] == 3


@pytest.mark.asyncio
async def test_commits_between_head_listing_failure_returns_none():
    gc = gitea_mod.GiteaClient.__new__(gitea_mod.GiteaClient)
    gc._initialized = True
    gc.get_commits = AsyncMock(return_value=None)
    gc.get_branch_head_sha = AsyncMock(return_value=None)

    assert await gc.get_commits_between("repo", "a" * 40, "gone-branch") is None
