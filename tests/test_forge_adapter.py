"""Forge adapter: one PR call, three forges."""

import json

import httpx
import pytest

from src.services.forge import (
    ForgeError,
    ForgeRepo,
    open_pull_request,
    parse_owner_repo,
    resolve_api_base,
)


def test_parse_owner_repo_handles_url_shapes():
    assert parse_owner_repo("https://github.com/acme/widget.git") == ("acme", "widget")
    assert parse_owner_repo("https://github.com/acme/widget") == ("acme", "widget")
    assert parse_owner_repo("https://git.example.com/acme/widget/") == (
        "acme",
        "widget",
    )


def test_resolve_api_base_per_forge():
    assert (
        resolve_api_base("https://github.com/a/b", "github") == "https://api.github.com"
    )
    # Self-hosted GitHub Enterprise uses /api/v3, not the SaaS host.
    assert (
        resolve_api_base("https://gh.corp.net/a/b", "github")
        == "https://gh.corp.net/api/v3"
    )
    assert (
        resolve_api_base("https://git.example.com/a/b", "gitea")
        == "https://git.example.com/api/v1"
    )
    assert (
        resolve_api_base("https://gitlab.com/a/b", "gitlab")
        == "https://gitlab.com/api/v4"
    )


def test_resolve_api_base_rejects_unknown_forge():
    with pytest.raises(ForgeError):
        resolve_api_base("https://example.com/a/b", "bitbucket")


def _capture(status=201, payload=None):
    """Return (transport, seen) recording the single request made.

    The body is captured PARSED, never as raw text: httpx changed its JSON
    separators across versions (0.28 emits compact {"a":1}, older emits
    {"a": 1}) and requirements.txt pins only httpx>=0.26.0.
    """
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["json"] = json.loads(request.read().decode())
        return httpx.Response(status, json=payload or {})

    return httpx.MockTransport(handler), seen


@pytest.mark.asyncio
async def test_github_pr_shape(monkeypatch):
    transport, seen = _capture(payload={"number": 7, "html_url": "https://gh/pr/7"})
    monkeypatch.setattr("src.services.forge._transport", transport, raising=False)

    target = ForgeRepo("github", "https://api.github.com", "acme", "widget", "tok")
    out = await open_pull_request(
        target, title="T", head="job/abc", base="develop", body="B"
    )

    assert seen["url"] == "https://api.github.com/repos/acme/widget/pulls"
    assert seen["headers"]["authorization"] == "Bearer tok"
    assert seen["json"] == {
        "title": "T",
        "head": "job/abc",
        "base": "develop",
        "body": "B",
    }
    assert out == {"number": 7, "url": "https://gh/pr/7"}


@pytest.mark.asyncio
async def test_gitea_pr_shape_matches_github_body_but_differs_on_auth(monkeypatch):
    transport, seen = _capture(payload={"number": 3, "html_url": "https://gt/pr/3"})
    monkeypatch.setattr("src.services.forge._transport", transport, raising=False)

    target = ForgeRepo(
        "gitea", "https://git.example.com/api/v1", "acme", "widget", "tok"
    )
    out = await open_pull_request(
        target, title="T", head="job/abc", base="develop", body="B"
    )

    assert seen["url"] == "https://git.example.com/api/v1/repos/acme/widget/pulls"
    # Gitea uses the "token" scheme, not Bearer.
    assert seen["headers"]["authorization"] == "token tok"
    # Body is byte-identical to GitHub's — that is the whole point.
    assert seen["json"] == {
        "title": "T",
        "head": "job/abc",
        "base": "develop",
        "body": "B",
    }
    assert out == {"number": 3, "url": "https://gt/pr/3"}


@pytest.mark.asyncio
async def test_gitlab_uses_encoded_project_path_and_its_own_field_names(monkeypatch):
    transport, seen = _capture(payload={"iid": 11, "web_url": "https://gl/mr/11"})
    monkeypatch.setattr("src.services.forge._transport", transport, raising=False)

    target = ForgeRepo("gitlab", "https://gitlab.com/api/v4", "acme", "widget", "tok")
    out = await open_pull_request(
        target, title="T", head="job/abc", base="develop", body="B"
    )

    # The project path must be URL-encoded into ONE segment; path segments 404.
    assert seen["url"] == (
        "https://gitlab.com/api/v4/projects/acme%2Fwidget/merge_requests"
    )
    assert seen["headers"]["private-token"] == "tok"
    assert seen["json"] == {
        "title": "T",
        "source_branch": "job/abc",
        "target_branch": "develop",
        "description": "B",
    }
    # GitLab calls it iid/web_url; the adapter normalizes both.
    assert out == {"number": 11, "url": "https://gl/mr/11"}


@pytest.mark.asyncio
async def test_error_response_raises_without_leaking_token(monkeypatch):
    transport, _ = _capture(status=422, payload={"message": "already exists"})
    monkeypatch.setattr("src.services.forge._transport", transport, raising=False)

    target = ForgeRepo("github", "https://api.github.com", "acme", "widget", "sekrit")
    with pytest.raises(ForgeError) as exc:
        await open_pull_request(target, title="T", head="h", base="b")

    assert "422" in str(exc.value)
    assert "sekrit" not in str(exc.value)


@pytest.mark.asyncio
async def test_github_validation_errors_detail_reaches_the_agent(monkeypatch):
    """GitHub's most common PR refusals hide the reason one level down.

    "head branch not pushed" and "no commits between" both come back as a
    generic ``{"message": "Validation Failed"}`` with the actionable text in
    ``errors[0].message``. Surfacing only the top-level message leaves the
    agent with nothing to act on.
    """
    transport, _ = _capture(
        status=422,
        payload={
            "message": "Validation Failed",
            "errors": [{"message": "No commits between develop and job/abc"}],
        },
    )
    monkeypatch.setattr("src.services.forge._transport", transport, raising=False)

    target = ForgeRepo("github", "https://api.github.com", "acme", "widget", "sekrit")
    with pytest.raises(ForgeError) as exc:
        await open_pull_request(target, title="T", head="job/abc", base="develop")

    text = str(exc.value)
    assert "No commits between develop and job/abc" in text
    assert "Validation Failed" in text
    assert "sekrit" not in text


@pytest.mark.asyncio
async def test_error_detail_tolerates_odd_errors_shapes(monkeypatch):
    """``errors`` is sometimes a list of bare strings, sometimes absent."""
    for payload in (
        {"message": "Validation Failed", "errors": ["plain string"]},
        {"message": "Validation Failed", "errors": []},
        {"message": "Validation Failed", "errors": {"not": "a list"}},
        {"message": "Validation Failed"},
    ):
        transport, _ = _capture(status=422, payload=payload)
        monkeypatch.setattr("src.services.forge._transport", transport, raising=False)
        target = ForgeRepo("github", "https://api.github.com", "a", "b", "sekrit")
        with pytest.raises(ForgeError) as exc:
            await open_pull_request(target, title="T", head="h", base="b")
        assert "Validation Failed" in str(exc.value)
        assert "sekrit" not in str(exc.value)


def test_forge_repo_repr_never_prints_the_token():
    """One uncaught non-ForgeError puts this dataclass in a traceback."""
    target = ForgeRepo("github", "https://api.github.com", "acme", "widget", "sekrit")

    assert "sekrit" not in repr(target)
    assert "widget" in repr(target)
    # Still readable through the attribute — only the repr is redacted.
    assert target.token == "sekrit"
