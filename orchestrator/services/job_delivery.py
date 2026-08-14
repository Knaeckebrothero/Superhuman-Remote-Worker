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


class ReviewDeliveryError(ValueError):
    """A review thread can no longer reproduce its recorded delivery."""


def _object(value: Any) -> dict[str, Any] | None:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, TypeError, ValueError):
            return None
    return value if isinstance(value, dict) else None


def parse_json_object(value: Any) -> dict[str, Any] | None:
    """Public JSONB-tolerant object coercion for job delivery consumers."""
    return _object(value)


def repository_host(url: str) -> str | None:
    """Return a credential-free lowercase host from web, SSH, or SCP Git URLs."""
    raw = str(url or "").strip()
    prefix, separator, scp_path = raw.partition(":")
    if "//" not in raw and separator and "/" in scp_path and "/" not in prefix:
        host = prefix.rsplit("@", 1)[-1]
    else:
        host = urlparse(raw).hostname
    return str(host).lower().rstrip(".") if host else None


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
        or repository_host(url) is None
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
    wanted_host = repository_host(pull_request.url)
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
        if repository_host(connection_url) != wanted_host:
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


def apply_review_delivery_branch(
    metadata: Any,
    datasources: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Apply a server-recorded review branch to its exact repository row.

    Ordinary sessions have no ``review_delivery`` marker and pass through
    unchanged. Review sessions fail closed when the connector disappeared or
    now names a different repository: silently cloning its default branch
    would put the reviewer in front of the wrong code.

    Returned rows are shallow copies only where an override is applied, so a
    request-scoped branch never mutates the shared datasource objects.
    """
    metadata_object = _object(metadata) or {}
    if "review_delivery" not in metadata_object:
        return datasources
    record = _object(metadata_object.get("review_delivery"))
    if record is None:
        raise ReviewDeliveryError("Review delivery metadata is malformed")

    datasource_id = record.get("datasource_id")
    forge = record.get("forge")
    expected_host = record.get("repository_host")
    repo = record.get("repo")
    branch = record.get("branch")
    if (
        not isinstance(datasource_id, str)
        or not datasource_id
        or not isinstance(forge, str)
        or forge not in SUPPORTED_FORGES
        or not isinstance(expected_host, str)
        or not expected_host
        or not isinstance(repo, str)
        or len([part for part in repo.strip("/").split("/") if part]) != 2
        or not isinstance(branch, str)
        or not branch.strip()
    ):
        raise ReviewDeliveryError("Review delivery metadata is malformed")

    for index, datasource in enumerate(datasources):
        if str(datasource.get("id") or "") != datasource_id:
            continue
        if datasource.get("type") != "repository":
            raise ReviewDeliveryError(
                "The review delivery connector is no longer a repository"
            )
        config = datasource.get("config")
        config = config if isinstance(config, dict) else {}
        connection_url = datasource.get("connection_url")
        try:
            owner, repo_name = parse_owner_repo(str(connection_url or ""))
        except ForgeError as exc:
            raise ReviewDeliveryError(
                "The review delivery connector no longer matches its repository"
            ) from exc
        if (
            str(config.get("forge") or "").strip().lower() != forge
            or repository_host(str(connection_url or "")) != expected_host
            or f"{owner}/{repo_name}".casefold() != repo.strip("/").casefold()
        ):
            raise ReviewDeliveryError(
                "The review delivery connector no longer matches its repository"
            )

        overridden = dict(datasource)
        overridden["default_branch"] = branch.strip()
        # The ordinary repository connector treats default_branch as
        # best-effort for backwards compatibility. A review session cannot:
        # its entire point is that this exact delivery is checked out.
        overridden["require_default_branch"] = True
        return [*datasources[:index], overridden, *datasources[index + 1 :]]

    raise ReviewDeliveryError("The review delivery connector is no longer attached")
