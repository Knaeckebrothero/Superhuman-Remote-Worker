"""Select the API client for a resolved writable project knowledge vault."""

from __future__ import annotations

from typing import Any

from src.services.forge import ForgeRepo, GitHubClient, resolve_api_base


class KbForgeConfigurationError(RuntimeError):
    """The resolved vault cannot be accessed with its configured forge auth."""


async def kb_client_for_repo(postgres_db: Any, gitea_client: Any, repo_ref: Any) -> Any:
    """Return a duck-typed KB client without putting secrets in ``repo_ref``.

    Gitea is the exact existing singleton and performs no credential lookup.
    GitHub resolves the descriptor's datasource UUID inside the orchestrator,
    where ``get_datasource`` decrypts the PAT for this call only.
    """
    if repo_ref.forge == "gitea":
        return gitea_client
    if repo_ref.forge != "github":
        raise KbForgeConfigurationError(f"Unsupported KB forge {repo_ref.forge!r}")
    if not repo_ref.credential_ref:
        raise KbForgeConfigurationError("GitHub KB credential is not configured")

    try:
        datasource = await postgres_db.get_datasource(repo_ref.credential_ref)
    except Exception as exc:
        raise KbForgeConfigurationError(
            "GitHub KB credential could not be loaded"
        ) from exc
    if not isinstance(datasource, dict) or datasource.get("type") != "kb":
        raise KbForgeConfigurationError("GitHub KB credential is not configured")
    credentials = datasource.get("credentials") or {}
    if not isinstance(credentials, dict):
        raise KbForgeConfigurationError("GitHub KB credential is not configured")
    token = str(credentials.get("token") or "").strip()
    if not token:
        raise KbForgeConfigurationError("GitHub KB credential is not configured")

    if not repo_ref.repo_url or not repo_ref.owner or not repo_ref.repo:
        raise KbForgeConfigurationError("GitHub KB repository location is invalid")
    return GitHubClient(
        ForgeRepo(
            forge="github",
            api_base=resolve_api_base(repo_ref.repo_url, "github"),
            owner=repo_ref.owner,
            repo=repo_ref.repo,
            token=token,
        )
    )


__all__ = ["KbForgeConfigurationError", "kb_client_for_repo"]
