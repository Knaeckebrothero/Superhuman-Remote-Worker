"""Forge adapter — one pull-request call across GitHub, Gitea and GitLab.

Gitea deliberately mirrors GitHub's REST API, so those two differ only in
API base and auth scheme. GitLab is the outlier: a URL-encoded project path
instead of owner/repo segments, different field names, and its own header.

A per-repository token is the universal credential here — every forge
supports one, unlike GitHub Apps which are GitHub-only.
"""

import logging
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import quote, urlparse

import httpx

logger = logging.getLogger(__name__)

SUPPORTED_FORGES = frozenset({"github", "gitea", "gitlab"})

# Overridden in tests with an httpx.MockTransport.
_transport: Optional[httpx.BaseTransport] = None


class ForgeError(RuntimeError):
    """Raised when a forge API call fails or is misconfigured."""


@dataclass(frozen=True)
class ForgeRepo:
    """Everything needed to call one repository's API."""

    forge: str
    api_base: str
    owner: str
    repo: str
    # repr=False: one uncaught non-ForgeError renders this dataclass into a
    # traceback, and tracebacks reach logs and the agent transcript.
    token: str = field(repr=False)


def parse_owner_repo(url: str) -> tuple[str, str]:
    """Return ``(owner, repo)`` from a repository URL."""
    path = urlparse(url.strip()).path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    parts = [p for p in path.split("/") if p]
    if len(parts) < 2:
        raise ForgeError(f"Cannot parse owner/repo from URL: {url!r}")
    # Trailing pair handles GitLab subgroups (group/sub/repo → sub, repo);
    # subgroup projects need the full path, handled in _gitlab_project_path.
    return parts[-2], parts[-1]


def resolve_api_base(url: str, forge: str) -> str:
    """Return the API root for ``url`` on ``forge``."""
    if forge not in SUPPORTED_FORGES:
        raise ForgeError(
            f"Unsupported forge {forge!r}; expected one of {sorted(SUPPORTED_FORGES)}"
        )
    parsed = urlparse(url.strip())
    if not parsed.hostname:
        raise ForgeError(f"Cannot parse host from URL: {url!r}")
    origin = f"{parsed.scheme or 'https'}://{parsed.netloc}"

    if forge == "github":
        # github.com routes to the SaaS API host; Enterprise uses /api/v3.
        if parsed.hostname.lower() in ("github.com", "www.github.com"):
            return "https://api.github.com"
        return f"{origin}/api/v3"
    if forge == "gitea":
        return f"{origin}/api/v1"
    return f"{origin}/api/v4"


def _gitlab_project_path(owner: str, repo: str) -> str:
    """URL-encode ``owner/repo`` into a single path segment.

    GitLab's project endpoint takes an ID or a fully URL-encoded path. Passing
    it as two path segments silently 404s.
    """
    return quote(f"{owner}/{repo}", safe="")


def _request_for(
    target: ForgeRepo, *, title: str, head: str, base: str, body: str
) -> tuple[str, dict, dict]:
    """Return ``(url, headers, json_body)`` for this forge's PR endpoint."""
    if target.forge == "gitlab":
        url = (
            f"{target.api_base}/projects/"
            f"{_gitlab_project_path(target.owner, target.repo)}/merge_requests"
        )
        headers = {"PRIVATE-TOKEN": target.token}
        payload = {
            "title": title,
            "source_branch": head,
            "target_branch": base,
            "description": body,
        }
        return url, headers, payload

    url = f"{target.api_base}/repos/{target.owner}/{target.repo}/pulls"
    scheme = "token" if target.forge == "gitea" else "Bearer"
    headers = {
        "Authorization": f"{scheme} {target.token}",
        "Accept": "application/json",
    }
    payload = {"title": title, "head": head, "base": base, "body": body}
    return url, headers, payload


async def open_pull_request(
    target: ForgeRepo, *, title: str, head: str, base: str, body: str = ""
) -> dict:
    """Open a pull/merge request. Returns ``{"number": int, "url": str}``."""
    if target.forge not in SUPPORTED_FORGES:
        raise ForgeError(f"Unsupported forge {target.forge!r}")

    url, headers, payload = _request_for(
        target, title=title, head=head, base=base, body=body
    )

    try:
        async with httpx.AsyncClient(timeout=30.0, transport=_transport) as client:
            resp = await client.post(url, headers=headers, json=payload)
    except httpx.HTTPError as exc:
        raise ForgeError(f"Could not reach {target.forge}: {exc}") from exc

    if resp.status_code not in (200, 201):
        # Include the forge's message but never the request headers/token.
        detail = ""
        try:
            data = resp.json()
            detail = str(data.get("message") or data.get("error") or "")[:200]
            # GitHub/Gitea bury the actionable reason one level down: the two
            # most common PR refusals ("head branch not pushed", "no commits
            # between X and Y") both arrive as a generic
            # {"message": "Validation Failed", "errors": [{"message": ...}]}.
            errors = data.get("errors")
            if isinstance(errors, list) and errors:
                first = errors[0]
                nested = first.get("message") if isinstance(first, dict) else first
                nested = str(nested or "")[:200]
                if nested and nested != detail:
                    detail = f"{detail}: {nested}" if detail else nested
        except (ValueError, AttributeError):
            pass
        raise ForgeError(
            f"{target.forge} refused the pull request "
            f"(HTTP {resp.status_code}){': ' + detail if detail else ''}"
        )

    data = resp.json()
    # GitHub/Gitea: number + html_url. GitLab: iid + web_url.
    number = data.get("number", data.get("iid"))
    link = data.get("html_url") or data.get("web_url") or ""
    if number is None:
        raise ForgeError(f"{target.forge} returned no PR number: {list(data)[:5]}")
    return {"number": int(number), "url": link}
