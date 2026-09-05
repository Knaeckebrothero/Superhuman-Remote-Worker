"""Forge/client selection for a project's writable knowledge vault."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.services.kb_forge import KbForgeConfigurationError, kb_client_for_repo
from orchestrator.services.kb_reindex import KbRepoRef
from shared.runtime.services.forge import GitHubClient


def _ref(forge: str, credential_ref: str | None = None) -> KbRepoRef:
    return KbRepoRef(
        forge=forge,
        repo_url=(
            "https://github.com/acme/vault.git"
            if forge == "github"
            else "http://gitea:3000/srw/project-kb.git"
        ),
        owner="acme" if forge == "github" else "srw",
        repo="vault" if forge == "github" else "project-kb",
        branch="main",
        credential_ref=credential_ref,
    )


@pytest.mark.asyncio
async def test_gitea_selection_returns_existing_singleton_without_credential_read():
    db = AsyncMock()
    gitea = MagicMock()

    selected = await kb_client_for_repo(db, gitea, _ref("gitea"))

    assert selected is gitea
    db.get_datasource.assert_not_awaited()


@pytest.mark.asyncio
async def test_github_selection_reads_token_from_datasource_handle_only():
    datasource_id = "55555555-6666-7777-8888-999999999999"
    db = AsyncMock()
    db.get_datasource.return_value = {
        "id": datasource_id,
        "type": "kb",
        "credentials": {"auth_method": "token", "token": "github-pat"},
    }

    selected = await kb_client_for_repo(db, MagicMock(), _ref("github", datasource_id))

    assert isinstance(selected, GitHubClient)
    db.get_datasource.assert_awaited_once_with(datasource_id)
    assert "github-pat" not in repr(selected)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("credential_ref", "row"),
    [
        (None, None),
        ("55555555-6666-7777-8888-999999999999", None),
        (
            "55555555-6666-7777-8888-999999999999",
            {"type": "kb", "credentials": {}},
        ),
    ],
)
async def test_github_missing_credential_fails_without_secret_detail(
    credential_ref, row
):
    db = AsyncMock()
    db.get_datasource.return_value = row

    with pytest.raises(KbForgeConfigurationError) as exc:
        await kb_client_for_repo(db, MagicMock(), _ref("github", credential_ref))

    assert "credential" in str(exc.value).lower()
    assert "token" not in str(exc.value).lower()


@pytest.mark.asyncio
async def test_unsupported_live_kb_forge_fails_closed():
    with pytest.raises(KbForgeConfigurationError, match="Unsupported KB forge"):
        await kb_client_for_repo(AsyncMock(), MagicMock(), _ref("gitlab"))
