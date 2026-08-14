"""Server-side resolution of a job's persisted source-repository delivery."""

import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from src.services.forge import (
    SUPPORTED_FORGES,
    ForgeError,
    ForgeRepo,
    parse_owner_repo,
    resolve_api_base,
)


@dataclass(frozen=True)
class JobPullRequest:
    """Validated structured record written by ``repo_open_pr``."""

    forge: str
    repo: str
    number: int
    url: str
    head: str
    base: str


def _object(value: Any) -> dict[str, Any] | None:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, TypeError, ValueError):
            return None
    return value if isinstance(value, dict) else None


def parse_job_pull_request(context: Any) -> JobPullRequest | None:
    """Validate the delivery record without treating model prose as metadata."""
    record = _object((_object(context) or {}).get("pull_request"))
    if record is None:
        return None

    forge = record.get("forge")
    repo = record.get("repo")
    number = record.get("number")
    url = record.get("url")
    head = record.get("head")
    base = record.get("base")
    if (
        not isinstance(forge, str)
        or forge not in SUPPORTED_FORGES
        or not isinstance(repo, str)
        or len([part for part in repo.strip("/").split("/") if part]) != 2
        or isinstance(number, bool)
        or not isinstance(number, int)
        or number < 1
        or not isinstance(url, str)
        or urlparse(url).scheme not in {"http", "https"}
        or not isinstance(head, str)
        or not head.strip()
        or not isinstance(base, str)
        or not base.strip()
    ):
        return None
    return JobPullRequest(
        forge=forge,
        repo=repo.strip("/"),
        number=number,
        url=url,
        head=head.strip(),
        base=base.strip(),
    )


def find_pull_request_repository(
    pull_request: JobPullRequest, datasources: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Find the attached connector named by the persisted delivery record."""
    wanted = pull_request.repo.casefold()
    for datasource in datasources:
        if datasource.get("type") != "repository":
            continue
        config = datasource.get("config")
        config = config if isinstance(config, dict) else {}
        if str(config.get("forge") or "").strip().lower() != pull_request.forge:
            continue
        connection_url = datasource.get("connection_url")
        if not isinstance(connection_url, str) or not connection_url:
            continue
        try:
            owner, repo = parse_owner_repo(connection_url)
        except ForgeError:
            continue
        if f"{owner}/{repo}".casefold() == wanted:
            return datasource
    return None


def forge_repo_from_datasource(datasource: dict[str, Any]) -> ForgeRepo:
    """Build a credential-bearing forge target that never leaves the server."""
    config = datasource.get("config")
    config = config if isinstance(config, dict) else {}
    forge = str(config.get("forge") or "").strip().lower()
    connection_url = datasource.get("connection_url")
    if not isinstance(connection_url, str) or not connection_url:
        raise ForgeError("Repository connector has no connection URL")
    owner, repo = parse_owner_repo(connection_url)
    credentials = datasource.get("credentials")
    credentials = credentials if isinstance(credentials, dict) else {}
    return ForgeRepo(
        forge=forge,
        api_base=resolve_api_base(connection_url, forge),
        owner=owner,
        repo=repo,
        token=str(credentials.get("token") or ""),
    )
