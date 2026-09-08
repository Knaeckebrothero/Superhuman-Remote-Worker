"""Pure validation/normalization of the non-secret and secret fields a
connector carries.

Extracted verbatim from ``orchestrator.main`` (R1.B03 lane D). Deliberately
free of every collaborator — no store, no clients, no logger, no application
global — because three independent callers share this authority: the
datasource CRUD surface, the project-provisioning KB vault plan, and the job
datasource payload builder. A second copy of "what may appear in a KB config"
is how the native-project marker gets stripped by one path and honored by
another.

The ``HTTPException``s raised here are the API's own 400 contract, so the
detail strings move with the code rather than being re-derived by a caller.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from fastapi import HTTPException

from shared.runtime.utils.ssh_key import (
    InvalidSSHKeyError,
    validate_private_key as _validate_ssh_private_key,
)


def normalize_datasource_credentials(
    credentials: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Validate and normalize secret fields in a datasource credentials dict.

    Currently this means: if an ``ssh_key`` is present, run it through
    :func:`validate_private_key`, which trims surrounding whitespace,
    normalizes line endings, and ensures the single trailing newline that
    OpenSSL/libcrypto requires. Raises ``HTTPException(400)`` if the key
    fails structural validation.
    """
    if not credentials:
        return credentials
    ssh_key = credentials.get("ssh_key")
    if ssh_key is None:
        return credentials
    try:
        credentials["ssh_key"] = _validate_ssh_private_key(ssh_key)
    except InvalidSSHKeyError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid ssh_key: {exc}") from exc
    return credentials


def normalize_kb_config(
    config: dict[str, Any] | None, *, stored: bool = False
) -> dict[str, Any]:
    """Validate the non-secret v1 config for an OKF KB datasource.

    ``root_path`` is relative to the configured repository root. Keep the
    accepted shape intentionally small so misspelled future-looking keys do not
    silently change indexing behavior.

    ``stored=True`` normalizes a config read back out of the database and
    carries the server-owned ``native_project_id`` marker through. User input
    is always normalized without it, so the marker cannot be hand-forged onto
    an external connector to steer it out of the sweep — and, read the other
    way, cannot be stripped off a project's own KB by editing its root path
    (that would drop the vault straight back into the external sweep and
    double-index it; knowledge-base/knowledge/features/knowledge_base_repo_separation.md §6).
    """
    from orchestrator.services.kb_datasources import NATIVE_PROJECT_CONFIG_KEY

    raw = dict(config or {})
    native_project = raw.pop(NATIVE_PROJECT_CONFIG_KEY, None) if stored else None
    unknown = sorted(set(raw) - {"root_path", "forge"})
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown KB config field(s): {', '.join(unknown)}",
        )

    value = raw.get("root_path", "")
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise HTTPException(status_code=400, detail="KB root_path must be a string")
    if "\x00" in value:
        raise HTTPException(status_code=400, detail="KB root_path contains NUL")

    normalized = value.strip().replace("\\", "/")
    if normalized.startswith("/") or urlparse(normalized).scheme:
        raise HTTPException(
            status_code=400,
            detail="KB root_path must be a relative repository path",
        )

    parts: list[str] = []
    for part in normalized.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise HTTPException(
                status_code=400,
                detail="KB root_path must not contain '..'",
            )
        parts.append(part)
    normalized_config = {"root_path": "/".join(parts)}
    forge = raw.get("forge")
    if forge is not None:
        if not isinstance(forge, str):
            raise HTTPException(status_code=400, detail="KB forge must be a string")
        from shared.runtime.services.forge import SUPPORTED_FORGES

        normalized_forge = forge.strip().lower()
        if normalized_forge not in SUPPORTED_FORGES:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Unsupported KB forge {normalized_forge!r}; expected one of "
                    f"{sorted(SUPPORTED_FORGES)}"
                ),
            )
        normalized_config["forge"] = normalized_forge
    if native_project:
        normalized_config[NATIVE_PROJECT_CONFIG_KEY] = str(native_project)
    return normalized_config


def normalize_repository_config(
    config: dict[str, Any] | None, connection_url: str | None
) -> dict[str, Any]:
    """Validate and default the ``forge`` field on a repository datasource.

    Host inference only covers the two SaaS hosts. A self-hosted Gitea and a
    self-hosted GitLab are indistinguishable by URL, so those must declare
    ``forge`` explicitly rather than be guessed at.
    """
    from shared.runtime.services.forge import SUPPORTED_FORGES  # noqa: PLC0415

    out = dict(config or {})
    forge = str(out.get("forge") or "").strip().lower()

    if not forge:
        host = (urlparse(connection_url or "").hostname or "").lower()
        if host in ("github.com", "www.github.com"):
            forge = "github"
        elif host in ("gitlab.com", "www.gitlab.com"):
            forge = "gitlab"
        else:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Repository connectors on a self-hosted host must declare "
                    f"'forge' explicitly (one of {sorted(SUPPORTED_FORGES)}) — "
                    "a self-hosted Gitea and GitLab cannot be told apart by URL"
                ),
            )

    if forge not in SUPPORTED_FORGES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported forge {forge!r}; expected one of {sorted(SUPPORTED_FORGES)}",
        )

    out["forge"] = forge
    return out


def validate_kb_repository_url(connection_url: str | None) -> str:
    """Require a safe network Git URL with no embedded credentials."""
    value = (connection_url or "").strip()
    if not value:
        raise HTTPException(
            status_code=400,
            detail="OKF Knowledge Base connectors require a repository URL",
        )
    from orchestrator.services.kb_git_source import validate_git_remote_url

    try:
        return validate_git_remote_url(value, allow_local=False)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        ) from exc


def validate_kb_repository_auth(
    connection_url: str,
    credentials: dict[str, Any] | None,
) -> None:
    """Reject unsafe OKF repository transport/auth combinations pre-persist."""
    from orchestrator.services.kb_git_source import validate_git_auth_configuration

    try:
        validate_git_auth_configuration(
            connection_url,
            credentials,
            allow_local=False,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
