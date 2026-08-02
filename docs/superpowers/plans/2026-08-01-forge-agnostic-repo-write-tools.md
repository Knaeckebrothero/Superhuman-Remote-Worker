# Forge-Agnostic Repository Write Tools Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give agents commit / push / pull / open-pull-request capability on any attached repository datasource, working identically on GitHub, Gitea, and GitLab with a single per-repo token.

**Architecture:** A pure `src/services/forge.py` adapter normalizes the three forges' pull-request APIs behind one function. Clone-time metadata (forge, token, owner, repo, API base) is carried to the tool layer on `workspace_manager.source_repo_meta`, mirroring the existing `source_repos` registry. A new `repo` tool category exposes four tools built on the already-shipped `GitManager` primitives, gated by the datasource's `read_only` flag.

**Tech Stack:** Python 3.12, `httpx.AsyncClient`, pytest. No new dependencies. **No schema migration** — `forge` lives in the existing `datasources.config` JSONB.

## Global Constraints

- **Supersedes** `docs/superpowers/plans/2026-08-01-github-app-credential-core.md`. The GitHub App is GitHub-only; a token is the universal credential. Do not implement the App here.
- **Token auth already works.** `clone_repository_datasources` rewrites the URL to `oauth2:<token>@host` (`src/core/datasource_setup.py`), which authenticates on all three forges. Do not change the clone auth path.
- **The token stays in `.git/config` for now.** The credential-seam hygiene fix (`scoped_git_push.md` Phase 1) is explicitly out of scope and nothing here depends on it.
- **New tool category must be `repo`, never `git`.** `datasource_tool_categories` strips a category to `[]` when no datasource of its type is attached (`src/core/datasource_setup.py:155`); reusing `git` would delete the workspace git tools on every job with no repo attached.
- **`has_datasource("repository")` does not work.** Repository datasources never enter `context.datasources` — `process_datasources` explicitly skips them with a warning (`src/core/datasource_setup.py:255`). Gate on `workspace_manager.source_repo_meta` instead.
- **`read_only` is not a security boundary.** The agent has a shell. Gating exists to prevent honest mistakes; never describe it as enforcement.
- **Python 3.12 is the CI gate.** Local runs on 3.14 are noisy.
- **`ruff format` runs in CI and rewrites SHAs.** Run `ruff check` and `ruff format` before committing.

---

### Task 1: Forge adapter

**Files:**
- Create: `src/services/forge.py`
- Test: `tests/test_forge_adapter.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `SUPPORTED_FORGES: frozenset[str]` = `{"github", "gitea", "gitlab"}`
  - `class ForgeError(RuntimeError)`
  - `@dataclass(frozen=True) class ForgeRepo` — fields `forge: str`, `api_base: str`, `owner: str`, `repo: str`, `token: str`
  - `def resolve_api_base(url: str, forge: str) -> str`
  - `def parse_owner_repo(url: str) -> tuple[str, str]`
  - `async def open_pull_request(target: ForgeRepo, *, title: str, head: str, base: str, body: str = "") -> dict` → `{"number": int, "url": str}`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_forge_adapter.py`:

```python
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
    assert parse_owner_repo("https://git.example.com/acme/widget/") == ("acme", "widget")


def test_resolve_api_base_per_forge():
    assert resolve_api_base("https://github.com/a/b", "github") == "https://api.github.com"
    # Self-hosted GitHub Enterprise uses /api/v3, not the SaaS host.
    assert resolve_api_base("https://gh.corp.net/a/b", "github") == "https://gh.corp.net/api/v3"
    assert resolve_api_base("https://git.example.com/a/b", "gitea") == "https://git.example.com/api/v1"
    assert resolve_api_base("https://gitlab.com/a/b", "gitlab") == "https://gitlab.com/api/v4"


def test_resolve_api_base_rejects_unknown_forge():
    with pytest.raises(ForgeError):
        resolve_api_base("https://example.com/a/b", "bitbucket")


def _capture(status=201, payload=None):
    """Return (transport, seen) recording the single request made.

    The body is captured PARSED, never as raw text: httpx changed its JSON
    separators across versions (0.28 emits compact ``{"a":1}``, older emits
    ``{"a": 1}``) and requirements.txt pins only ``httpx>=0.26.0``. Asserting
    on serialized text would make these tests depend on which httpx happened
    to get installed.
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_forge_adapter.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.services.forge'`

- [ ] **Step 3: Write the implementation**

Create `src/services/forge.py`:

```python
"""Forge adapter — one pull-request call across GitHub, Gitea and GitLab.

Gitea deliberately mirrors GitHub's REST API, so those two differ only in
API base and auth scheme. GitLab is the outlier: a URL-encoded project path
instead of owner/repo segments, different field names, and its own header.

A per-repository token is the universal credential here — every forge
supports one, unlike GitHub Apps which are GitHub-only.
"""

import logging
from dataclasses import dataclass
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
    token: str


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
        except ValueError:
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_forge_adapter.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Lint and commit**

```bash
ruff check src/services/forge.py tests/test_forge_adapter.py
ruff format src/services/forge.py tests/test_forge_adapter.py
git add src/services/forge.py tests/test_forge_adapter.py
git commit -m "feat(forge): one pull-request adapter for github, gitea and gitlab"
```

---

### Task 2: `forge` field on the repository datasource

**Files:**
- Modify: `orchestrator/main.py` — add `_normalize_repository_config`, call it from `create_datasource` and `update_datasource`
- Test: `tests/test_repository_forge_config.py` (create)

**Interfaces:**
- Consumes: `SUPPORTED_FORGES` from Task 1.
- Produces: `datasources.config` carrying `{"forge": "github" | "gitea" | "gitlab"}`, defaulted by host when unambiguous.
  - `def _normalize_repository_config(config: dict | None, connection_url: str | None) -> dict`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_repository_forge_config.py`:

```python
"""forge field normalization on repository datasources."""

import pytest
from fastapi import HTTPException

from orchestrator.main import _normalize_repository_config


def test_explicit_forge_is_kept():
    out = _normalize_repository_config(
        {"forge": "gitea"}, "https://git.example.com/acme/widget"
    )
    assert out["forge"] == "gitea"


def test_github_com_is_inferred():
    out = _normalize_repository_config({}, "https://github.com/acme/widget")
    assert out["forge"] == "github"


def test_gitlab_com_is_inferred():
    out = _normalize_repository_config(None, "https://gitlab.com/acme/widget")
    assert out["forge"] == "gitlab"


def test_self_hosted_host_cannot_be_inferred():
    """A self-hosted Gitea and a self-hosted GitLab look identical by URL."""
    with pytest.raises(HTTPException) as exc:
        _normalize_repository_config({}, "https://git.example.com/acme/widget")
    assert exc.value.status_code == 400
    assert "forge" in str(exc.value.detail)


def test_unknown_forge_is_rejected():
    with pytest.raises(HTTPException) as exc:
        _normalize_repository_config(
            {"forge": "bitbucket"}, "https://bitbucket.org/a/b"
        )
    assert exc.value.status_code == 400


def test_unrelated_config_keys_survive():
    out = _normalize_repository_config(
        {"forge": "github", "path": "sub/dir"}, "https://github.com/a/b"
    )
    assert out["path"] == "sub/dir"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_repository_forge_config.py -v`
Expected: FAIL — `ImportError: cannot import name '_normalize_repository_config'`

- [ ] **Step 3: Write the implementation**

In `orchestrator/main.py`, add next to `_normalize_kb_config` (around line 17433):

```python
def _normalize_repository_config(
    config: dict[str, Any] | None, connection_url: str | None
) -> dict[str, Any]:
    """Validate and default the ``forge`` field on a repository datasource.

    Host inference only covers the two SaaS hosts. A self-hosted Gitea and a
    self-hosted GitLab are indistinguishable by URL, so those must declare
    ``forge`` explicitly rather than be guessed at.
    """
    from src.services.forge import SUPPORTED_FORGES  # noqa: PLC0415

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
```

Then wire it into both endpoints' **type-dispatch chains**. Both chains end in a
catch-all that rejects any non-empty config for types other than `kb`/`email`/`mcp`
(`create_datasource` ~17690, `update_datasource` ~17902), so a `repository` branch
must be added *before* that catch-all or `{"forge": ...}` always 400s.

In `create_datasource`, add a branch immediately before the final `else:`:

```python
    elif body.type == "repository":
        datasource_config = _normalize_repository_config(
            body.config, body.connection_url
        )
```

In `update_datasource`, the stored row is bound as **`existing_ds`** by
`require_datasource_owner`. Replace the catch-all `elif datasource_config:` (~17902)
with a repository branch ahead of it, using the **effective** connection URL (the
incoming one if present, else the stored row's):

```python
    elif (existing_ds.get("type") or "") == "repository":
        effective_url = body.connection_url or existing_ds.get("connection_url")
        datasource_config = _normalize_repository_config(
            datasource_config, effective_url
        )
    elif datasource_config:
        raise HTTPException(
            status_code=400,
            detail=(
                "Connector config is only supported for OKF Knowledge Bases "
                "and email connectors"
            ),
        )
```

`urlparse` is already imported at `orchestrator/main.py:22` — do not add another import.

**Add endpoint-level tests, not just helper tests.** The helper tests above pass
even when the endpoint rejects the config outright — that gap is exactly what a
pure-unit test misses. Add to `tests/test_repository_forge_config.py` a test that
POSTs a repository connector with `config={"forge": "github"}` through the real
route and asserts a 2xx plus a persisted `config.forge`, following the client
fixture pattern in `tests/test_datasource_redesign.py`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_repository_forge_config.py tests/test_datasource_redesign.py -v`
Expected: PASS. The second suite guards the other datasource types.

- [ ] **Step 5: Lint and commit**

```bash
ruff check orchestrator/main.py tests/test_repository_forge_config.py
ruff format orchestrator/main.py tests/test_repository_forge_config.py
git add orchestrator/main.py tests/test_repository_forge_config.py
git commit -m "feat(repo): validate and default the forge field on repository connectors"
```

---

### Task 3: Carry repo metadata to the tool layer

**Files:**
- Modify: `src/core/workspace.py:325, 403` (add the `source_repo_meta` store and property)
- Modify: `src/core/datasource_setup.py` (populate it in `clone_repository_datasources`)
- Test: `tests/test_datasource_repo_clone.py`

**Interfaces:**
- Consumes: `parse_owner_repo`, `resolve_api_base` from Task 1.
- Produces: `workspace_manager.source_repo_meta: dict[str, dict]`, keyed by the same clone-directory name as `source_repos`, each value:
  ```python
  {"forge": str, "api_base": str, "owner": str, "repo": str,
   "token": str, "read_only": bool, "default_branch": str | None}
  ```

- [ ] **Step 1: Write the failing test**

Add to `tests/test_datasource_repo_clone.py`, inside `class TestBackendClone`:

```python
    def test_token_clone_records_forge_metadata_for_tools(self):
        """Tools need forge/token/owner/repo; source_repos only carries GitManager."""
        ws = make_workspace_manager()
        ds = token_ds(
            name="SRW Repository",
            url="https://github.com/Knaeckebrothero/Superhuman-Remote-Worker",
            config={"forge": "github"},
            default_branch="develop",
            read_only=False,
        )
        with patch(
            "src.managers.git_manager.GitManager.clone", return_value=MagicMock()
        ):
            clone_repository_datasources([ds], ws)

        meta = ws.source_repo_meta["Superhuman-Remote-Worker"]
        assert meta["forge"] == "github"
        assert meta["api_base"] == "https://api.github.com"
        assert meta["owner"] == "Knaeckebrothero"
        assert meta["repo"] == "Superhuman-Remote-Worker"
        assert meta["token"] == "tok123"
        assert meta["read_only"] is False
        assert meta["default_branch"] == "develop"

    def test_repo_metadata_marks_read_only_datasources(self):
        ws = make_workspace_manager()
        ds = token_ds(config={"forge": "github"}, read_only=True)
        with patch(
            "src.managers.git_manager.GitManager.clone", return_value=MagicMock()
        ):
            clone_repository_datasources([ds], ws)
        assert ws.source_repo_meta["repo"]["read_only"] is True
```

> `make_workspace_manager()` returns a `MagicMock`, so `ws.source_repo_meta` will
> auto-create as a Mock attribute and the assertions would pass vacuously. Add
> `ws.source_repo_meta = {}` to `make_workspace_manager()` alongside the existing
> `ws.source_repos = {}` line so the test exercises real dict writes.

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/test_datasource_repo_clone.py -v -k metadata`
Expected: FAIL — `KeyError: 'Superhuman-Remote-Worker'` (the dict is never written)

- [ ] **Step 3: Write the implementation**

In `src/core/workspace.py`, beside `self._source_repos` (line 325):

```python
        self._source_repo_meta: dict[str, dict] = {}
```

and beside the `source_repos` property (line 403):

```python
    @property
    def source_repo_meta(self) -> dict[str, dict]:
        """Forge/auth metadata per cloned repo, keyed like ``source_repos``.

        ``source_repos`` holds GitManager instances, which know how to run git
        but nothing about the forge's API. The repo tools need both.
        """
        return self._source_repo_meta
```

In `src/core/datasource_setup.py`, inside `clone_repository_datasources`, in the
`if git_mgr:` block right after `workspace_manager.source_repos[repo_name] = git_mgr`:

```python
                try:
                    from ..services.forge import parse_owner_repo, resolve_api_base

                    forge = str((ds.get("config") or {}).get("forge") or "").lower()
                    raw_url = ds.get("connection_url", "")
                    owner, repo_slug = parse_owner_repo(raw_url)
                    workspace_manager.source_repo_meta[repo_name] = {
                        "forge": forge,
                        "api_base": resolve_api_base(raw_url, forge),
                        "owner": owner,
                        "repo": repo_slug,
                        "token": (creds or {}).get("token", ""),
                        "read_only": bool(ds.get("read_only")),
                        "default_branch": branch,
                    }
                except Exception as e:
                    # A metadata failure must not fail the clone; the repo is
                    # still usable through the shell and the read-only git tools.
                    logger.warning(
                        "Could not record forge metadata for repos/%s: %s",
                        repo_name,
                        e,
                    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_datasource_repo_clone.py -v`
Expected: PASS, including all pre-existing clone tests.

- [ ] **Step 5: Lint and commit**

```bash
ruff check src/core/workspace.py src/core/datasource_setup.py tests/test_datasource_repo_clone.py
ruff format src/core/workspace.py src/core/datasource_setup.py tests/test_datasource_repo_clone.py
git add src/core/workspace.py src/core/datasource_setup.py tests/test_datasource_repo_clone.py
git commit -m "feat(repo): carry forge metadata from clone to the tool layer"
```

---

### Task 4: The `repo` toolkit and its registry wiring

**Files:**
- Create: `src/tools/repo/__init__.py`
- Create: `src/tools/repo/repo_tools.py`
- Modify: `src/core/datasource_setup.py:93` (add the `repository` entry to `DATASOURCE_TOOL_MAP`)
- Modify: `src/tools/registry.py:23` (import) and `:551` area (wiring block)
- Test: `tests/test_repo_tools.py` (create), `tests/test_datasource_tool_categories.py`

**Interfaces:**
- Consumes: `ForgeRepo` / `open_pull_request` (Task 1); `source_repo_meta` (Task 3); the shipped `GitManager.commit/push/pull/checkout_branch/current_branch`.
- Produces: four LangChain tools — `repo_commit`, `repo_push`, `repo_pull`, `repo_open_pr` — plus `create_repo_tools(context)` and `get_repo_metadata()`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_repo_tools.py`:

```python
"""repo_* tools: thin, read_only-gated wrappers over GitManager + the forge adapter."""

from unittest.mock import MagicMock, patch

import pytest

from src.tools.context import ToolContext
from src.tools.repo import create_repo_tools


def make_context(read_only=False, forge="github"):
    ws = MagicMock()
    git_mgr = MagicMock()
    git_mgr.commit.return_value = True
    git_mgr.push.return_value = True
    git_mgr.pull.return_value = True
    git_mgr.current_branch.return_value = "job/abc12345"
    ws.source_repos = {"widget": git_mgr}
    ws.source_repo_meta = {
        "widget": {
            "forge": forge,
            "api_base": "https://api.github.com",
            "owner": "acme",
            "repo": "widget",
            "token": "tok",
            "read_only": read_only,
            "default_branch": "develop",
        }
    }
    return ToolContext(workspace_manager=ws), git_mgr


def get_tool(tools, name):
    return next(t for t in tools if t.name == name)


@pytest.mark.asyncio
async def test_repo_commit_commits_in_the_named_clone():
    context, git_mgr = make_context()
    tool = get_tool(create_repo_tools(context), "repo_commit")

    out = await tool.ainvoke({"repo": "widget", "message": "fix: thing"})

    git_mgr.commit.assert_called_once_with("fix: thing")
    assert "fix: thing" in out or "committed" in out.lower()


@pytest.mark.asyncio
async def test_repo_push_pushes_current_branch():
    context, git_mgr = make_context()
    tool = get_tool(create_repo_tools(context), "repo_push")

    await tool.ainvoke({"repo": "widget"})

    git_mgr.push.assert_called_once()


@pytest.mark.asyncio
async def test_write_tools_refuse_on_read_only_datasource():
    context, git_mgr = make_context(read_only=True)
    tools = create_repo_tools(context)

    for name in ("repo_commit", "repo_push", "repo_open_pr"):
        tool = get_tool(tools, name)
        kwargs = {"repo": "widget"}
        if name == "repo_commit":
            kwargs["message"] = "m"
        if name == "repo_open_pr":
            kwargs.update({"title": "T", "base": "develop"})
        out = await tool.ainvoke(kwargs)
        assert "read-only" in out.lower()

    git_mgr.commit.assert_not_called()
    git_mgr.push.assert_not_called()


@pytest.mark.asyncio
async def test_repo_pull_is_allowed_on_read_only_datasource():
    context, git_mgr = make_context(read_only=True)
    tool = get_tool(create_repo_tools(context), "repo_pull")

    await tool.ainvoke({"repo": "widget"})

    git_mgr.pull.assert_called_once()


@pytest.mark.asyncio
async def test_repo_open_pr_calls_the_forge_adapter():
    context, _ = make_context()
    tool = get_tool(create_repo_tools(context), "repo_open_pr")

    with patch(
        "src.tools.repo.repo_tools.open_pull_request",
        return_value={"number": 9, "url": "https://gh/pr/9"},
    ) as mock_pr:
        out = await tool.ainvoke(
            {"repo": "widget", "title": "T", "base": "develop", "body": "B"}
        )

    target = mock_pr.call_args[0][0]
    assert target.forge == "github"
    assert target.owner == "acme"
    assert target.token == "tok"
    # head defaults to the branch currently checked out in that clone.
    assert mock_pr.call_args[1]["head"] == "job/abc12345"
    assert "https://gh/pr/9" in out


@pytest.mark.asyncio
async def test_unknown_repo_name_is_a_clear_error():
    context, _ = make_context()
    tool = get_tool(create_repo_tools(context), "repo_push")

    out = await tool.ainvoke({"repo": "nope"})

    assert "nope" in out
    assert "widget" in out  # names the ones that DO exist
```

Add to `tests/test_datasource_tool_categories.py`:

```python
def test_repository_maps_to_repo_category_not_git():
    """Reusing 'git' would strip the workspace git tools when no repo is attached."""
    from src.core.datasource_setup import datasource_tool_categories

    cats = datasource_tool_categories(
        [{"type": "repository", "name": "r", "project_read_only": False}]
    )
    assert "repo_push" in cats["repo"]
    assert "git" not in cats


def test_read_only_repository_gets_only_pull():
    from src.core.datasource_setup import datasource_tool_categories

    cats = datasource_tool_categories(
        [{"type": "repository", "name": "r", "project_read_only": True}]
    )
    assert cats["repo"] == ["repo_pull"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_repo_tools.py tests/test_datasource_tool_categories.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.tools.repo'`

- [ ] **Step 3: Write the implementation**

Create `src/tools/repo/repo_tools.py`:

```python
"""Repository write tools for attached repository datasources.

These operate on the clones under ``repos/<name>/`` registered in
``workspace_manager.source_repos``, using the forge metadata recorded
alongside them in ``source_repo_meta``.

``read_only`` gating here prevents honest mistakes. It is NOT a security
boundary — the agent has a shell and can run git directly.
"""

import logging
from typing import Any, List, Optional

from langchain_core.tools import tool

from ...services.forge import ForgeError, ForgeRepo, open_pull_request
from ..context import ToolContext

logger = logging.getLogger(__name__)

REPO_TOOLS_METADATA = {
    "repo_commit": {
        "category": "repo",
        "short_description": "Stage and commit changes in an attached repository.",
    },
    "repo_push": {
        "category": "repo",
        "short_description": "Push the current branch of an attached repository.",
    },
    "repo_pull": {
        "category": "repo",
        "short_description": "Fast-forward pull in an attached repository.",
    },
    "repo_open_pr": {
        "category": "repo",
        "short_description": "Open a pull/merge request for an attached repository.",
    },
}


def create_repo_tools(context: ToolContext) -> List[Any]:
    """Create the repo_* tools bound to this job's cloned repositories."""
    ws = context.workspace_manager

    def _resolve(repo: str) -> tuple[Any, dict] | str:
        """Return (git_manager, meta) or an error string naming valid repos."""
        repos = getattr(ws, "source_repos", {}) or {}
        meta_all = getattr(ws, "source_repo_meta", {}) or {}
        if repo not in repos:
            known = ", ".join(sorted(repos)) or "(none attached)"
            return f"Unknown repository {repo!r}. Attached repositories: {known}"
        return repos[repo], meta_all.get(repo, {})

    def _refuse_if_read_only(meta: dict, repo: str) -> Optional[str]:
        if meta.get("read_only"):
            return (
                f"Repository {repo!r} is attached read-only; "
                "only repo_pull is available."
            )
        return None

    @tool
    async def repo_commit(repo: str, message: str) -> str:
        """Stage all changes and commit them in an attached repository.

        Args:
            repo: Clone-directory name, as listed in datasources.md.
            message: Commit message.
        """
        resolved = _resolve(repo)
        if isinstance(resolved, str):
            return resolved
        git_mgr, meta = resolved
        refusal = _refuse_if_read_only(meta, repo)
        if refusal:
            return refusal
        if git_mgr.commit(message):
            return f"Committed in {repo}: {message}"
        return f"Nothing to commit in {repo} (or the commit failed — check repo_status)."

    @tool
    async def repo_push(repo: str, branch: Optional[str] = None) -> str:
        """Push a branch of an attached repository to its remote.

        Args:
            repo: Clone-directory name.
            branch: Branch to push; defaults to the currently checked-out one.
        """
        resolved = _resolve(repo)
        if isinstance(resolved, str):
            return resolved
        git_mgr, meta = resolved
        refusal = _refuse_if_read_only(meta, repo)
        if refusal:
            return refusal
        target = branch or git_mgr.current_branch()
        if git_mgr.push(branch=target):
            return f"Pushed {target} to {repo}'s remote."
        return (
            f"Push of {target} to {repo} failed. If the remote rejected it, the "
            "branch is probably protected — push a job branch instead."
        )

    @tool
    async def repo_pull(repo: str, branch: Optional[str] = None) -> str:
        """Fast-forward pull in an attached repository.

        Args:
            repo: Clone-directory name.
            branch: Branch to pull; defaults to the current one.
        """
        resolved = _resolve(repo)
        if isinstance(resolved, str):
            return resolved
        git_mgr, _meta = resolved
        if git_mgr.pull(branch=branch):
            return f"Pulled latest changes into {repo}."
        return f"Pull in {repo} failed (diverged history, or no remote configured)."

    @tool
    async def repo_open_pr(
        repo: str,
        title: str,
        base: str,
        body: str = "",
        head: Optional[str] = None,
    ) -> str:
        """Open a pull request (merge request on GitLab) for an attached repository.

        Push the branch first — the forge rejects a PR whose head does not exist.

        Args:
            repo: Clone-directory name.
            title: PR title.
            base: Branch to merge INTO (e.g. "develop").
            body: PR description.
            head: Branch to merge FROM; defaults to the current one.
        """
        resolved = _resolve(repo)
        if isinstance(resolved, str):
            return resolved
        git_mgr, meta = resolved
        refusal = _refuse_if_read_only(meta, repo)
        if refusal:
            return refusal
        if not meta.get("forge"):
            return (
                f"Repository {repo!r} has no forge recorded, so its API cannot be "
                "called. Set 'forge' on the connector and re-run the job."
            )

        source = head or git_mgr.current_branch()
        target = ForgeRepo(
            forge=meta["forge"],
            api_base=meta["api_base"],
            owner=meta["owner"],
            repo=meta["repo"],
            token=meta.get("token", ""),
        )
        try:
            result = await open_pull_request(
                target, title=title, head=source, base=base, body=body
            )
        except ForgeError as exc:
            return f"Could not open the pull request: {exc}"
        return f"Opened #{result['number']} ({source} → {base}): {result['url']}"

    return [repo_commit, repo_push, repo_pull, repo_open_pr]
```

Create `src/tools/repo/__init__.py`:

```python
"""Repository toolkit — write operations on attached repository datasources.

Distinct from the `git` toolkit, which is read-only and targets the internal
workspace repo. See docs/features/self_development_workflow.md.
"""

from typing import Any, Dict, List

from ..context import ToolContext


def create_repo_tools(context: ToolContext) -> List[Any]:
    """Create all repo tools with injected context."""
    from .repo_tools import create_repo_tools as _create

    return _create(context)


def get_repo_metadata() -> Dict[str, Dict[str, Any]]:
    """Get metadata for all repo tools."""
    from .repo_tools import REPO_TOOLS_METADATA

    return REPO_TOOLS_METADATA
```

In `src/core/datasource_setup.py`, add to `DATASOURCE_TOOL_MAP` (after the `webdav`
entry):

```python
    "repository": {
        "category": "repo",
        "read": ["repo_pull"],
        "write": ["repo_commit", "repo_push", "repo_pull", "repo_open_pr"],
    },
```

In `src/tools/registry.py`, add the import beside the webdav one (line ~23):

```python
from .repo import create_repo_tools, get_repo_metadata
```

register its metadata beside line ~84:

```python
TOOL_REGISTRY.update(get_repo_metadata())
```

and add the wiring block after the WebDAV block (~line 564):

```python
    # Repository datasource write tools. NOTE: unlike the other datasource
    # toolkits this cannot use context.has_datasource() — repository
    # datasources never enter context.datasources (process_datasources skips
    # them); the clones live on workspace_manager instead.
    if "repo" in tools_by_category:
        ws = context.workspace_manager
        if not getattr(ws, "source_repos", None):
            logger.warning("Repo tools require at least one cloned repository")
        else:
            try:
                repo_tools = create_repo_tools(context)
                requested = set(tools_by_category["repo"])
                for tool in repo_tools:
                    if tool.name in requested:
                        all_tools.append(tool)
                        logger.debug(f"Loaded repo tool: {tool.name}")
            except Exception as e:
                logger.warning(f"Could not load repo tools: {e}")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_repo_tools.py tests/test_datasource_tool_categories.py -v`
Expected: PASS

- [ ] **Step 5: Run the full tool-registry suite for regressions**

Run: `python -m pytest tests/ -v -k "registry or tool_categories or tool_registry"`
Expected: PASS. This is the guard that the new category did not disturb the `git` toolkit.

- [ ] **Step 6: Lint and commit**

```bash
ruff check src/tools/repo/ src/tools/registry.py src/core/datasource_setup.py tests/test_repo_tools.py
ruff format src/tools/repo/ src/tools/registry.py src/core/datasource_setup.py tests/test_repo_tools.py
git add src/tools/repo/ src/tools/registry.py src/core/datasource_setup.py tests/test_repo_tools.py tests/test_datasource_tool_categories.py
git commit -m "feat(repo): commit/push/pull/open-pr tools under a new repo category"
```

---

### Task 5: Cockpit — forge selector and token guidance

**Files:**
- Modify: `cockpit/src/app/views/datasources/datasource-list.component.ts` (the repository branch of the connector form)
- Modify: `cockpit/src/app/core/models/api.model.ts` (add `forge` to the repository config type)
- Modify: `cockpit/src/assets/i18n/en.json` and `cockpit/src/assets/i18n/de-DE.json`
- Test: `cockpit/src/app/views/datasources/datasource-list.component.spec.ts`

**Interfaces:**
- Consumes: the `forge` values validated in Task 2.
- Produces: no new API surface — the form posts `config.forge` alongside the existing fields.

- [ ] **Step 1: Read the existing repository branch of the form**

```bash
rg -n "ssh_key|auth_method|repository" cockpit/src/app/views/datasources/datasource-list.component.ts
```

Follow whatever control pattern the sibling datasource types already use in that
component — do not introduce a new form idiom.

- [ ] **Step 2: Write the failing spec**

Add to the form's existing `.spec.ts`, matching its established harness:

```typescript
it('requires an explicit forge for a self-hosted host', () => {
  component.form.patchValue({
    type: 'repository',
    connectionUrl: 'https://git.example.com/acme/widget',
    forge: '',
  });
  expect(component.form.valid).toBeFalse();
});

it('defaults forge to github for github.com', () => {
  component.form.patchValue({
    type: 'repository',
    connectionUrl: 'https://github.com/acme/widget',
  });
  component.onConnectionUrlChange();
  expect(component.form.value.forge).toBe('github');
});
```

- [ ] **Step 3: Run the spec to verify it fails**

Run: `cd cockpit && npx vitest run --reporter verbose -t forge`
Expected: FAIL

- [ ] **Step 4: Implement the form changes**

- Add a `forge` select with options **GitHub / Gitea / GitLab**, shown only when `type === 'repository'`.
- On connection-URL change, default `forge` to `github` for `github.com` and `gitlab` for `gitlab.com`; leave blank otherwise and mark the control required.
- Add help text under the token field:
  - **GitHub:** "Create a fine-grained PAT limited to this one repository, with Contents: write and Pull requests: write."
  - **GitLab:** "Create a project access token on this project with the `api` scope."
  - **Gitea:** "⚠️ Gitea tokens are scoped by permission, not by repository — a token from your own account can reach every repo you can access. Create a dedicated bot account, add it as a collaborator on just this repository, and use its token."

The Gitea warning is required, not optional: a user pasting their own Gitea PAT
silently hands over their whole account.

- [ ] **Step 5: Run the spec and the build**

```bash
cd cockpit && npx vitest run
npm i --no-save @monaco-editor/loader && npx ng build
```

Expected: specs PASS; build succeeds within the scss and 2.75 MB bundle budgets (both hard-fail CI).

- [ ] **Step 6: Add i18n strings and commit**

Add the new keys to **both** `en.json` and `de-DE.json` — parity is enforced.

```bash
git add cockpit/src/app cockpit/src/assets/i18n
git commit -m "feat(cockpit): forge selector and per-forge token guidance for repo connectors"
```

---

### Task 6: Update the `repo-contribution` skill

**Files:**
- Modify: `docs/skills/repo-contribution/SKILL.md`

**Interfaces:**
- Consumes: the four tools from Task 4.
- Produces: no code.

- [ ] **Step 1: Rewrite the mechanics steps**

Replace the raw-shell git commands in steps 2, 7 and the `Don't` list with the tools:

- Step 5 (implement) is unchanged.
- Step 7 becomes: `repo_commit(repo=..., message=...)` then `repo_push(repo=...)`.
- Add a step 8: `repo_open_pr(repo=..., title=..., base="<default branch>", body=<contents of output/pr.md>)` — **after** the push, because the forge rejects a PR whose head branch does not exist yet.
- Keep `output/pr.md` — it remains the artifact the PR body is written from, and it survives if the PR call fails.
- Keep branching via the shell (`git -C repos/<name> checkout -b`); there is no `repo_checkout` tool and none is needed.
- Update the `Don't` list: replace "Force-push or rewrite history" guidance to note that `repo_push` has no force option by design.

- [ ] **Step 2: Verify budgets and that it still parses**

```bash
python3 -c "
from src.core.skill_format import parse_skill_md
meta, body = parse_skill_md(open('docs/skills/repo-contribution/SKILL.md').read())
print('name:', meta['name'])
print('description chars:', len(meta['description']), '(limit 1024)')
print('lines:', len(body.splitlines()), '(limit 500)')
"
```

Expected: parses; description ≤1024 chars; body <500 lines.

- [ ] **Step 3: Commit**

```bash
git add docs/skills/repo-contribution/SKILL.md
git commit -m "docs(skill): drive repo-contribution through the repo_* tools"
```

---

## Verification before calling this done

```bash
python -m pytest tests/test_forge_adapter.py tests/test_repository_forge_config.py \
  tests/test_repo_tools.py tests/test_datasource_repo_clone.py \
  tests/test_datasource_tool_categories.py tests/test_datasource_redesign.py -v
ruff check src/ orchestrator/ tests/
cd cockpit && npx vitest run
```

Then the live gate on dev — the only thing that proves the forge adapter against a real API:

1. Replace the SRW Repository connector's token with a **fine-grained PAT** scoped to that repo (`Contents: write`, `Pull requests: write`), and set `config.forge = "github"`.
2. Give **KurortEngine** a token — it currently has `credentials={}` and cannot push at all.
3. Run a job that branches, edits a file, `repo_commit`, `repo_push`, `repo_open_pr` against `develop`.
4. Confirm the PR exists on GitHub with the body from `output/pr.md`, and the job sits in `pending_review`.
5. Confirm a `read_only` connector refuses `repo_push` with the read-only message.

**Gitea and GitLab remain unverified against live servers after this plan.** The
adapter's shapes are covered by unit tests only. Do not claim those forges work
until someone has run step 3 against a real instance of each — the most likely
failure is GitLab's project-path encoding or a token scope, neither of which a
mocked transport can catch.
