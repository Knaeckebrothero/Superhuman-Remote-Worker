"""Forge adapter: one PR call, three forges."""

import json
from pathlib import Path

import httpx
import pytest

from shared.runtime.services.forge import (
    ForgeError,
    ForgeRepo,
    GitHubClient,
    forge_web_url_matches_connector,
    get_pull_request_status,
    open_pull_request,
    parse_owner_repo,
    resolve_api_base,
)


def test_gitea_web_url_accepts_only_the_server_configured_internal_public_pair(
    monkeypatch,
):
    monkeypatch.setenv("GITEA_INTERNAL_URL", "http://srw-gitea:3000")
    monkeypatch.setenv("GITEA_URL", "https://git.srw.works")

    assert forge_web_url_matches_connector(
        "https://git.srw.works/acme/widget/pulls/7",
        "http://srw-gitea:3000/acme/widget.git",
        "gitea",
    )
    assert not forge_web_url_matches_connector(
        "https://git.srw.works/acme/widget/pulls/7",
        "https://other-gitea.example/acme/widget.git",
        "gitea",
    )
    assert not forge_web_url_matches_connector(
        "https://attacker.example/acme/widget/pulls/7",
        "http://srw-gitea:3000/acme/widget.git",
        "gitea",
    )


def test_gitea_web_url_alias_fails_closed_without_both_server_urls(monkeypatch):
    monkeypatch.setenv("GITEA_URL", "https://git.srw.works")
    monkeypatch.delenv("GITEA_INTERNAL_URL", raising=False)

    assert not forge_web_url_matches_connector(
        "https://git.srw.works/acme/widget/pulls/7",
        "http://srw-gitea:3000/acme/widget.git",
        "gitea",
    )
    assert forge_web_url_matches_connector(
        "https://github.com/acme/widget/pull/7",
        "https://github.com/acme/widget.git",
        "github",
    )


def test_parse_owner_repo_handles_url_shapes():
    assert parse_owner_repo("https://github.com/acme/widget.git") == ("acme", "widget")
    assert parse_owner_repo("https://github.com/acme/widget") == ("acme", "widget")
    assert parse_owner_repo("https://git.example.com/acme/widget/") == (
        "acme",
        "widget",
    )
    assert parse_owner_repo("git@github.com:acme/widget.git") == ("acme", "widget")


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
    assert (
        resolve_api_base("git@github.com:acme/widget.git", "github")
        == "https://api.github.com"
    )


def test_resolve_api_base_never_carries_clone_url_credentials():
    assert (
        resolve_api_base("https://oauth2:sekrit@git.example.test/a/b.git", "gitea")
        == "https://git.example.test/api/v1"
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


def _capture_get(status=200, payload=None):
    """Return a transport recording one credential-bearing status GET."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        return httpx.Response(status, json=payload or {})

    return httpx.MockTransport(handler), seen


@pytest.mark.asyncio
async def test_github_pr_status_distinguishes_merged_from_closed(monkeypatch):
    transport, seen = _capture_get(
        payload={
            "number": 7,
            "html_url": "https://github.com/acme/widget/pull/7",
            "state": "closed",
            "merged": True,
            "draft": False,
            "head": {"ref": "feature/review", "sha": "a" * 40},
            "base": {"ref": "develop"},
        }
    )
    monkeypatch.setattr(
        "shared.runtime.services.forge._transport", transport, raising=False
    )

    target = ForgeRepo("github", "https://api.github.com", "acme", "widget", "tok")
    out = await get_pull_request_status(target, 7)

    assert seen["method"] == "GET"
    assert seen["url"] == "https://api.github.com/repos/acme/widget/pulls/7"
    assert seen["headers"]["authorization"] == "Bearer tok"
    assert out == {
        "number": 7,
        "url": "https://github.com/acme/widget/pull/7",
        "state": "merged",
        "head": "feature/review",
        "base": "develop",
        "head_sha": "a" * 40,
        "draft": False,
    }


@pytest.mark.asyncio
async def test_gitea_pr_status_normalizes_open(monkeypatch):
    transport, seen = _capture_get(
        payload={
            "number": 3,
            "html_url": "https://git.example.test/acme/widget/pulls/3",
            "state": "open",
            "merged": False,
            "head": {"ref": "job/abc", "sha": "b" * 40},
            "base": {"ref": "main"},
        }
    )
    monkeypatch.setattr(
        "shared.runtime.services.forge._transport", transport, raising=False
    )

    target = ForgeRepo(
        "gitea", "https://git.example.test/api/v1", "acme", "widget", "tok"
    )
    out = await get_pull_request_status(target, 3)

    assert seen["url"] == "https://git.example.test/api/v1/repos/acme/widget/pulls/3"
    assert seen["headers"]["authorization"] == "token tok"
    assert out["state"] == "open"
    assert out["draft"] is False


@pytest.mark.asyncio
async def test_gitlab_pr_status_uses_iid_and_normalizes_locked_as_closed(monkeypatch):
    transport, seen = _capture_get(
        payload={
            "iid": 11,
            "web_url": "https://gitlab.com/acme/widget/-/merge_requests/11",
            "state": "locked",
            "source_branch": "job/abc",
            "target_branch": "main",
            "sha": "c" * 40,
            "draft": True,
        }
    )
    monkeypatch.setattr(
        "shared.runtime.services.forge._transport", transport, raising=False
    )

    target = ForgeRepo("gitlab", "https://gitlab.com/api/v4", "acme", "widget", "tok")
    out = await get_pull_request_status(target, 11)

    assert seen["url"] == (
        "https://gitlab.com/api/v4/projects/acme%2Fwidget/merge_requests/11"
    )
    assert seen["headers"]["private-token"] == "tok"
    assert out == {
        "number": 11,
        "url": "https://gitlab.com/acme/widget/-/merge_requests/11",
        "state": "closed",
        "head": "job/abc",
        "base": "main",
        "head_sha": "c" * 40,
        "draft": True,
    }


@pytest.mark.asyncio
async def test_pr_status_errors_do_not_leak_the_token(monkeypatch):
    transport, _ = _capture_get(status=403, payload={"message": "forbidden"})
    monkeypatch.setattr(
        "shared.runtime.services.forge._transport", transport, raising=False
    )

    target = ForgeRepo("github", "https://api.github.com", "acme", "widget", "sekrit")
    with pytest.raises(ForgeError) as exc:
        await get_pull_request_status(target, 7)

    assert "403" in str(exc.value)
    assert "sekrit" not in str(exc.value)


@pytest.mark.asyncio
async def test_github_pr_shape(monkeypatch):
    transport, seen = _capture(payload={"number": 7, "html_url": "https://gh/pr/7"})
    monkeypatch.setattr(
        "shared.runtime.services.forge._transport", transport, raising=False
    )

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
    monkeypatch.setattr(
        "shared.runtime.services.forge._transport", transport, raising=False
    )

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
    monkeypatch.setattr(
        "shared.runtime.services.forge._transport", transport, raising=False
    )

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
    monkeypatch.setattr(
        "shared.runtime.services.forge._transport", transport, raising=False
    )

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
    monkeypatch.setattr(
        "shared.runtime.services.forge._transport", transport, raising=False
    )

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
        monkeypatch.setattr(
            "shared.runtime.services.forge._transport", transport, raising=False
        )
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


def _github_client(token: str = "sekrit") -> GitHubClient:
    return GitHubClient(
        ForgeRepo(
            "github",
            "https://api.github.com",
            "acme",
            "vault",
            token,
        )
    )


@pytest.mark.asyncio
async def test_github_list_tree_uses_recursive_git_tree_api(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        return httpx.Response(
            200,
            json={
                "tree": [
                    {"path": "knowledge/nested/note.md", "type": "blob", "sha": "a1"},
                    {"path": "knowledge/nested", "type": "tree", "sha": "b2"},
                ],
                "truncated": False,
            },
        )

    monkeypatch.setattr(
        "shared.runtime.services.forge._transport",
        httpx.MockTransport(handler),
        raising=False,
    )

    tree = await _github_client().list_tree("vault", "feature/kb")

    assert seen["url"] == (
        "https://api.github.com/repos/acme/vault/git/trees/feature%2Fkb?recursive=1"
    )
    assert seen["headers"]["authorization"] == "Bearer sekrit"
    assert tree == [
        {"path": "knowledge/nested/note.md", "type": "blob", "sha": "a1"},
        {"path": "knowledge/nested", "type": "tree", "sha": "b2"},
    ]


@pytest.mark.asyncio
async def test_github_change_files_creates_one_file_with_contents_api(monkeypatch):
    transport, seen = _capture(
        payload={"content": {"sha": "new"}, "commit": {"sha": "commit"}}
    )
    monkeypatch.setattr(
        "shared.runtime.services.forge._transport", transport, raising=False
    )

    ok = await _github_client().change_files(
        "vault",
        "main",
        [
            {
                "path": "knowledge/nested/note.md",
                "content_b64": "Ym9keQ==",
                "operation": "create",
            }
        ],
        "kb: note",
    )

    assert ok is True
    assert seen["url"] == (
        "https://api.github.com/repos/acme/vault/contents/knowledge/nested/note.md"
    )
    assert seen["json"] == {
        "message": "kb: note",
        "content": "Ym9keQ==",
        "branch": "main",
    }


@pytest.mark.asyncio
async def test_github_change_files_threads_blob_sha_for_update(monkeypatch):
    transport, seen = _capture(payload={"commit": {"sha": "commit"}})
    monkeypatch.setattr(
        "shared.runtime.services.forge._transport", transport, raising=False
    )

    ok = await _github_client().change_files(
        "vault",
        "main",
        [
            {
                "path": "knowledge/note.md",
                "content_b64": "bmV3",
                "operation": "update",
                "sha": "old-blob",
            }
        ],
        "kb: note",
    )

    assert ok is True
    assert seen["json"]["sha"] == "old-blob"


@pytest.mark.asyncio
async def test_github_change_files_looks_up_missing_update_sha(monkeypatch):
    seen: list[tuple[str, str, dict | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.read().decode()) if request.content else None
        seen.append((request.method, str(request.url), payload))
        if request.method == "GET":
            return httpx.Response(200, json={"sha": "looked-up-blob"})
        return httpx.Response(200, json={"commit": {"sha": "commit"}})

    monkeypatch.setattr(
        "shared.runtime.services.forge._transport",
        httpx.MockTransport(handler),
        raising=False,
    )

    ok = await _github_client().change_files(
        "vault",
        "feature/kb",
        [
            {
                "path": "knowledge/note.md",
                "content_b64": "bmV3",
                "operation": "update",
            }
        ],
        "kb: note",
    )

    assert ok is True
    assert seen == [
        (
            "GET",
            "https://api.github.com/repos/acme/vault/contents/knowledge/note.md?ref=feature%2Fkb",
            None,
        ),
        (
            "PUT",
            "https://api.github.com/repos/acme/vault/contents/knowledge/note.md",
            {
                "message": "kb: note",
                "content": "bmV3",
                "branch": "feature/kb",
                "sha": "looked-up-blob",
            },
        ),
    ]


@pytest.mark.asyncio
async def test_github_change_files_rejects_multi_file_commit_without_network(
    monkeypatch,
):
    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("multi-file GitHub write reached the network")

    monkeypatch.setattr(
        "shared.runtime.services.forge._transport",
        httpx.MockTransport(handler),
        raising=False,
    )
    one = {"path": "knowledge/a.md", "content_b64": "YQ=="}
    two = {"path": "knowledge/b.md", "content_b64": "Yg=="}

    assert (
        await _github_client().change_files("vault", "main", [one, two], "not atomic")
        is False
    )


@pytest.mark.asyncio
async def test_github_branch_head_encodes_slashes(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"commit": {"sha": "head-sha"}})

    monkeypatch.setattr(
        "shared.runtime.services.forge._transport",
        httpx.MockTransport(handler),
        raising=False,
    )

    head = await _github_client().get_branch_head_sha("vault", "feature/kb")

    assert head == "head-sha"
    assert seen["url"] == (
        "https://api.github.com/repos/acme/vault/branches/feature%2Fkb"
    )


@pytest.mark.asyncio
async def test_github_archive_download_writes_response_bytes(monkeypatch, tmp_path):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, content=b"tar-gz-bytes")

    monkeypatch.setattr(
        "shared.runtime.services.forge._transport",
        httpx.MockTransport(handler),
        raising=False,
    )
    destination = tmp_path / "vault.tar.gz"

    ok = await _github_client().download_repo_archive(
        "vault", "feature/kb", str(destination)
    )

    assert ok is True
    assert Path(destination).read_bytes() == b"tar-gz-bytes"
    assert seen["url"] == (
        "https://api.github.com/repos/acme/vault/tarball/feature%2Fkb"
    )


@pytest.mark.asyncio
async def test_github_client_failures_do_not_expose_token(monkeypatch, caplog):
    monkeypatch.setattr(
        "shared.runtime.services.forge._transport",
        httpx.MockTransport(
            lambda _request: httpx.Response(403, json={"message": "denied"})
        ),
        raising=False,
    )

    assert await _github_client("never-log-this").list_tree("vault", "main") is None
    assert "never-log-this" not in caplog.text
