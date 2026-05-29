"""Unit test for GiteaClient.change_files (batch multi-file single commit)."""

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
