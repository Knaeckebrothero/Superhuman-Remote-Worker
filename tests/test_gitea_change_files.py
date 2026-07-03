"""Unit tests for GiteaClient.change_files and get_commits."""

import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

_orch_dir = str(Path(__file__).parent.parent / "orchestrator")
if _orch_dir not in sys.path:
    sys.path.insert(0, _orch_dir)
os.environ.setdefault("VECTOR_DB_URL", "postgresql://test@localhost/test")

from services import gitea as gitea_mod  # noqa: E402


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
