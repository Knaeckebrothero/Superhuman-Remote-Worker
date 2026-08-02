# GitHub App Credential Core Implementation Plan

> ## ⚠️ SUPERSEDED 2026-08-01 — DO NOT EXECUTE
>
> Replaced by `2026-08-01-forge-agnostic-repo-write-tools.md`.
>
> **Why:** this plan builds a GitHub-only credential mechanism. GitHub Apps have no
> equivalent on Gitea or GitLab, so they cannot be the basis of a repository
> datasource meant to work on whatever forge a customer runs. A per-repository
> **token** is the universal credential — every forge supports one — and token
> git-over-HTTPS auth already works in `clone_repository_datasources` today.
>
> The App remains worth building **later, for GitHub users only**, for auto-rotating
> credentials and an unforgeable `[bot]` identity. It slots behind the same
> `auth_method` seam, so nothing in the superseding plan blocks it. Tasks 1 and 4
> here (token minting, `commit_identity`) stay valid as written if that happens.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a repository datasource authenticate as a user-registered GitHub App, minting short-lived installation tokens on demand, with the token never persisted into `.git/config`.

**Architecture:** A new `orchestrator/services/github_app.py` mints installation tokens (RS256 JWT → `POST /app/installations/{id}/access_tokens`) behind a small expiry-aware cache. The dispatch-time clone path stops embedding secrets in the remote URL and instead materializes a `0600` git-credentials store file wired in via `credential.helper`, so every later git invocation picks up auth with no environment threading. Commit identity becomes per-datasource instead of hardcoded.

**Tech Stack:** Python 3.12, `PyJWT[crypto]>=2.8.0` (already in `orchestrator/requirements.txt`), `httpx.AsyncClient`, `cryptography>=42.0` (already in `requirements.txt`), pytest.

## Global Constraints

- **BYO App model.** Each repository datasource carries its own `app_id`, `installation_id`, and `private_key`. There is no system-wide App, no install flow, and no installation-id discovery in this plan.
- **Credentials live in `datasources.credentials`** (AES-256-GCM encrypted at rest, existing machinery). No new secret store, no Vault key.
- **Coexistence, not migration.** `auth_method: "github_app"` lands alongside the existing `ssh` and `token` methods. Existing datasource rows must keep working untouched; no data migration, no schema change.
- **The token must never enter `.git/config` or a remote URL.** `git remote -v` must show a clean URL. This is the whole point of the credential seam.
- **Posture 1 (agent-readable) is accepted.** The `0600` store file is readable by the agent. Do not add obfuscation that implies otherwise.
- **Python 3.12 is the CI gate.** Local runs on 3.14 are noisy; trust CI.
- **`ruff format` runs in CI and rewrites SHAs on push.** Run `ruff check` and `ruff format` locally before committing.

---

### Task 1: Installation-token minting service

**Files:**
- Create: `orchestrator/services/github_app.py`
- Test: `tests/test_github_app.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `class GitHubAppError(RuntimeError)`
  - `@dataclass(frozen=True) class InstallationToken` with fields `token: str`, `expires_at: float` (epoch seconds)
  - `def build_app_jwt(app_id: str, private_key_pem: str, *, now: float | None = None) -> str`
  - `async def mint_installation_token(app_id: str, private_key_pem: str, installation_id: str, *, now: float | None = None) -> InstallationToken`
  - `async def get_installation_token(app_id: str, private_key_pem: str, installation_id: str, *, now: float | None = None) -> InstallationToken` — cached variant; Tasks 3 and 5 call **this one**, never `mint_installation_token` directly
  - `def clear_token_cache() -> None` — test hook

- [ ] **Step 1: Write the failing tests**

Create `tests/test_github_app.py`:

```python
"""GitHub App installation-token minting."""

import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from orchestrator.services.github_app import (
    GitHubAppError,
    build_app_jwt,
    clear_token_cache,
    get_installation_token,
    mint_installation_token,
)


@pytest.fixture
def rsa_keypair():
    """Return (private_pem_str, public_key) for signing/verifying App JWTs."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    return pem, key.public_key()


@pytest.fixture(autouse=True)
def _clear_cache():
    clear_token_cache()
    yield
    clear_token_cache()


def test_build_app_jwt_claims(rsa_keypair):
    pem, public_key = rsa_keypair
    now = 1_700_000_000.0

    token = build_app_jwt("123456", pem, now=now)
    claims = jwt.decode(token, public_key, algorithms=["RS256"], options={"verify_exp": False})

    assert claims["iss"] == "123456"
    # Backdated to absorb clock skew against GitHub.
    assert claims["iat"] == int(now) - 60
    # Inside GitHub's 10-minute ceiling.
    assert claims["exp"] == int(now) + 540
    assert claims["exp"] - claims["iat"] < 600


@pytest.mark.asyncio
async def test_mint_posts_to_installation_endpoint(rsa_keypair, monkeypatch):
    pem, _ = rsa_keypair
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["accept"] = request.headers.get("accept")
        return httpx.Response(
            201,
            json={"token": "ghs_faketoken", "expires_at": "2026-08-01T13:00:00Z"},
        )

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        "orchestrator.services.github_app._transport", transport, raising=False
    )

    result = await mint_installation_token("123456", pem, "987", now=1_700_000_000.0)

    assert seen["url"] == "https://api.github.com/app/installations/987/access_tokens"
    assert seen["auth"].startswith("Bearer ")
    assert seen["accept"] == "application/vnd.github+json"
    assert result.token == "ghs_faketoken"
    assert result.expires_at > 0


@pytest.mark.asyncio
async def test_mint_raises_on_auth_failure(rsa_keypair, monkeypatch):
    pem, _ = rsa_keypair

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "Bad credentials"})

    monkeypatch.setattr(
        "orchestrator.services.github_app._transport",
        httpx.MockTransport(handler),
        raising=False,
    )

    with pytest.raises(GitHubAppError) as exc:
        await mint_installation_token("123456", pem, "987")
    # The message must name the status so a misconfigured App is diagnosable.
    assert "401" in str(exc.value)


@pytest.mark.asyncio
async def test_get_installation_token_reuses_cached_token(rsa_keypair, monkeypatch):
    pem, _ = rsa_keypair
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            201,
            json={"token": f"ghs_{calls['n']}", "expires_at": "2026-08-01T13:00:00Z"},
        )

    monkeypatch.setattr(
        "orchestrator.services.github_app._transport",
        httpx.MockTransport(handler),
        raising=False,
    )

    now = time.time()
    first = await get_installation_token("123456", pem, "987", now=now)
    second = await get_installation_token("123456", pem, "987", now=now + 10)

    assert first.token == second.token
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_get_installation_token_refreshes_inside_margin(rsa_keypair, monkeypatch):
    pem, _ = rsa_keypair
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            201,
            json={"token": f"ghs_{calls['n']}", "expires_at": "2026-08-01T13:00:00Z"},
        )

    monkeypatch.setattr(
        "orchestrator.services.github_app._transport",
        httpx.MockTransport(handler),
        raising=False,
    )

    now = time.time()
    first = await get_installation_token("123456", pem, "987", now=now)
    # Jump to inside the 300s refresh margin: a cached token this close to
    # expiry must NOT be handed out, or a long push races expiry mid-transfer.
    later = first.expires_at - 60
    second = await get_installation_token("123456", pem, "987", now=later)

    assert second.token != first.token
    assert calls["n"] == 2
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_github_app.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'orchestrator.services.github_app'`

- [ ] **Step 3: Write the implementation**

Create `orchestrator/services/github_app.py`:

```python
"""GitHub App installation-token minting (BYO-App model).

Each repository datasource carries its own App credentials. We authenticate
as the App with a short-lived RS256 JWT, exchange it for an installation
token (~1 hour), and cache that token until shortly before it expires.

The token is deliberately never persisted anywhere durable: it is written to
an ephemeral 0600 store file on the workspace (see the clone path) and
re-minted on demand. Long jobs outlive a single token, so every consumer
must call get_installation_token() rather than holding one.
"""

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import httpx
import jwt

logger = logging.getLogger(__name__)

_GITHUB_API = "https://api.github.com"

# GitHub caps App JWTs at 10 minutes; 9 leaves headroom for clock skew.
_JWT_TTL_S = 540
# Backdate iat so a fast clock on our side does not produce a future-dated
# token that GitHub rejects.
_JWT_BACKDATE_S = 60
# Hand back a cached token only while it has more than this left. A push of a
# large branch can take minutes; expiring mid-transfer is a confusing failure.
_EXPIRY_MARGIN_S = 300

# Overridden in tests with an httpx.MockTransport.
_transport: Optional[httpx.BaseTransport] = None

_cache: dict[tuple[str, str], "InstallationToken"] = {}


class GitHubAppError(RuntimeError):
    """Raised when an installation token cannot be minted."""


@dataclass(frozen=True)
class InstallationToken:
    """A minted installation token and its absolute expiry (epoch seconds)."""

    token: str
    expires_at: float


def clear_token_cache() -> None:
    """Drop every cached installation token. Test hook."""
    _cache.clear()


def build_app_jwt(
    app_id: str, private_key_pem: str, *, now: Optional[float] = None
) -> str:
    """Return an RS256 JWT authenticating as the App itself."""
    issued = int(now if now is not None else time.time())
    payload = {
        "iat": issued - _JWT_BACKDATE_S,
        "exp": issued + _JWT_TTL_S,
        "iss": str(app_id),
    }
    try:
        return jwt.encode(payload, private_key_pem, algorithm="RS256")
    except Exception as exc:  # invalid/unsupported key material
        raise GitHubAppError(f"Could not sign App JWT: {exc}") from exc


def _parse_expiry(raw: str | None, *, now: float) -> float:
    """Parse GitHub's ISO-8601 expires_at; fall back to a conservative hour."""
    if not raw:
        return now + 3600
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        logger.warning("Unparseable installation-token expires_at %r", raw)
        return now + 3600


async def mint_installation_token(
    app_id: str,
    private_key_pem: str,
    installation_id: str,
    *,
    now: Optional[float] = None,
) -> InstallationToken:
    """Exchange an App JWT for an installation token. Always hits the API."""
    stamp = now if now is not None else time.time()
    app_jwt = build_app_jwt(app_id, private_key_pem, now=stamp)
    url = f"{_GITHUB_API}/app/installations/{installation_id}/access_tokens"
    headers = {
        "Authorization": f"Bearer {app_jwt}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    try:
        async with httpx.AsyncClient(timeout=30.0, transport=_transport) as client:
            resp = await client.post(url, headers=headers)
    except httpx.HTTPError as exc:
        raise GitHubAppError(
            f"Could not reach GitHub to mint an installation token: {exc}"
        ) from exc

    if resp.status_code not in (200, 201):
        # Never log the response body: it can echo request headers.
        raise GitHubAppError(
            f"GitHub refused the installation-token request "
            f"(HTTP {resp.status_code}) for installation {installation_id}. "
            "Check the App ID, installation ID, and private key."
        )

    body = resp.json()
    token = body.get("token")
    if not token:
        raise GitHubAppError("GitHub returned no token in the response body")

    return InstallationToken(
        token=token, expires_at=_parse_expiry(body.get("expires_at"), now=stamp)
    )


async def get_installation_token(
    app_id: str,
    private_key_pem: str,
    installation_id: str,
    *,
    now: Optional[float] = None,
) -> InstallationToken:
    """Return a cached installation token, minting when it is near expiry."""
    stamp = now if now is not None else time.time()
    key = (str(app_id), str(installation_id))

    cached = _cache.get(key)
    if cached is not None and cached.expires_at - stamp > _EXPIRY_MARGIN_S:
        return cached

    minted = await mint_installation_token(
        app_id, private_key_pem, installation_id, now=stamp
    )
    _cache[key] = minted
    return minted
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_github_app.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Lint and commit**

```bash
ruff check orchestrator/services/github_app.py tests/test_github_app.py
ruff format orchestrator/services/github_app.py tests/test_github_app.py
git add orchestrator/services/github_app.py tests/test_github_app.py
git commit -m "feat(github-app): mint installation tokens with an expiry-aware cache"
```

---

### Task 2: Accept and validate `github_app` credentials

**Files:**
- Modify: `orchestrator/main.py:17405-17426` (`_normalize_datasource_credentials`)
- Test: `tests/test_github_app_datasource.py` (create)

**Interfaces:**
- Consumes: nothing from Task 1 (validation is structural only — no network call).
- Produces: a validated credentials shape that Task 3 reads:
  ```jsonc
  {
    "auth_method": "github_app",
    "app_id": "123456",           // digits, string
    "installation_id": "987654",  // digits, string
    "private_key": "-----BEGIN RSA PRIVATE KEY-----\n..."
  }
  ```

- [ ] **Step 1: Write the failing tests**

Create `tests/test_github_app_datasource.py`:

```python
"""Validation of github_app repository-datasource credentials."""

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException

from orchestrator.main import _normalize_datasource_credentials


def _pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


def test_github_app_credentials_pass_and_normalize_key():
    creds = {
        "auth_method": "github_app",
        "app_id": "123456",
        "installation_id": "987654",
        "private_key": "  " + _pem().strip() + "  ",
    }
    out = _normalize_datasource_credentials(creds)
    # Same normalization contract as ssh_key: one trailing newline, LF endings.
    assert out["private_key"].endswith("\n")
    assert "\r" not in out["private_key"]
    assert out["app_id"] == "123456"


@pytest.mark.parametrize("missing", ["app_id", "installation_id", "private_key"])
def test_github_app_credentials_require_every_field(missing):
    creds = {
        "auth_method": "github_app",
        "app_id": "123456",
        "installation_id": "987654",
        "private_key": _pem(),
    }
    del creds[missing]
    with pytest.raises(HTTPException) as exc:
        _normalize_datasource_credentials(creds)
    assert exc.value.status_code == 400
    assert missing in str(exc.value.detail)


def test_github_app_rejects_non_numeric_ids():
    creds = {
        "auth_method": "github_app",
        "app_id": "not-a-number",
        "installation_id": "987654",
        "private_key": _pem(),
    }
    with pytest.raises(HTTPException) as exc:
        _normalize_datasource_credentials(creds)
    assert exc.value.status_code == 400


def test_github_app_rejects_garbage_private_key():
    creds = {
        "auth_method": "github_app",
        "app_id": "123456",
        "installation_id": "987654",
        "private_key": "definitely not a PEM",
    }
    with pytest.raises(HTTPException) as exc:
        _normalize_datasource_credentials(creds)
    assert exc.value.status_code == 400


def test_existing_ssh_credentials_still_normalize():
    """Coexistence guard: the ssh path must be untouched by this change.

    ``validate_private_key`` accepts ``-----BEGIN RSA PRIVATE KEY-----``, so
    the generated PEM is a valid ssh_key for validation purposes.
    """
    out = _normalize_datasource_credentials({"ssh_key": "  " + _pem().strip() + "  "})
    assert out["ssh_key"].endswith("\n")
    assert "\r" not in out["ssh_key"]


def test_credentials_without_auth_method_are_untouched():
    creds = {"token": "ghp_something"}
    assert _normalize_datasource_credentials(creds) == {"token": "ghp_something"}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_github_app_datasource.py -v`
Expected: FAIL — the `github_app` tests fail because no branch validates them (the ssh and no-auth_method tests should already pass).

- [ ] **Step 3: Write the implementation**

In `orchestrator/main.py`, replace the body of `_normalize_datasource_credentials` (currently at line 17405) with:

```python
def _normalize_datasource_credentials(
    credentials: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Validate and normalize secret fields in a datasource credentials dict.

    Two shapes are handled:

    - ``ssh_key`` present → run through :func:`validate_private_key`, which
      trims whitespace, normalizes line endings, and ensures the single
      trailing newline OpenSSL/libcrypto requires.
    - ``auth_method == "github_app"`` → require ``app_id``,
      ``installation_id`` and ``private_key``; the key goes through the same
      PEM validator (GitHub App keys are PKCS#1 RSA PEM, an accepted marker).

    Raises ``HTTPException(400)`` on structural failure.
    """
    if not credentials:
        return credentials

    if credentials.get("auth_method") == "github_app":
        for field in ("app_id", "installation_id", "private_key"):
            if not str(credentials.get(field) or "").strip():
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"github_app credentials require a non-empty '{field}'"
                    ),
                )
        for field in ("app_id", "installation_id"):
            value = str(credentials[field]).strip()
            if not value.isdigit():
                raise HTTPException(
                    status_code=400,
                    detail=f"github_app '{field}' must be numeric (got {value!r})",
                )
            credentials[field] = value
        try:
            credentials["private_key"] = _validate_ssh_private_key(
                credentials["private_key"]
            )
        except InvalidSSHKeyError as exc:
            raise HTTPException(
                status_code=400, detail=f"Invalid github_app private_key: {exc}"
            ) from exc
        return credentials

    ssh_key = credentials.get("ssh_key")
    if ssh_key is None:
        return credentials
    try:
        credentials["ssh_key"] = _validate_ssh_private_key(ssh_key)
    except InvalidSSHKeyError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid ssh_key: {exc}") from exc
    return credentials
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_github_app_datasource.py tests/test_ssh_key_utils.py -v`
Expected: PASS. The ssh tests confirm coexistence.

- [ ] **Step 5: Lint and commit**

```bash
ruff check orchestrator/main.py tests/test_github_app_datasource.py
ruff format orchestrator/main.py tests/test_github_app_datasource.py
git add orchestrator/main.py tests/test_github_app_datasource.py
git commit -m "feat(github-app): validate github_app datasource credentials"
```

---

### Task 3: Clone via a credential store file, never a token in the URL

**Files:**
- Modify: `src/core/datasource_setup.py:849-1005` (`clone_repository_datasources`) — add a `github_app` branch alongside the existing `ssh` and `token` branches
- Test: `tests/test_datasource_repo_clone.py`

**Interfaces:**
- Consumes: `get_installation_token(app_id, private_key_pem, installation_id) -> InstallationToken` (Task 1); the validated credentials shape (Task 2).
- Produces:
  - A helper `_write_git_credential_store(backend, ds_slug, repo_url, token) -> str` returning the absolute store-file path on the workspace.
  - Cloned repos whose `.git/config` contains a clean HTTPS remote URL and a `credential.helper` pointing at that store file.

**Why a config-based helper rather than `GIT_ASKPASS`:** `GitManager._run_git` executes git through `backend.shell_run` and does not inject per-command environment variables. A `credential.helper` lives in git config, so every later git invocation on that repo picks it up with no env threading — `GitManager.push()` keeps working unmodified.

- [ ] **Step 1: Write the failing tests**

Add this class to `tests/test_datasource_repo_clone.py`, reusing the existing
module-level `make_workspace_manager()` helper and the `patch(...GitManager.clone)`
convention already used by `TestBackendClone`:

```python
class TestGitHubAppClone:
    """github_app auth: token reaches git via a helper file, never the URL."""

    @staticmethod
    def app_ds(name="SRW Repository", **extra):
        return {
            "type": "repository",
            "name": name,
            "connection_url": "https://github.com/Knaeckebrothero/Superhuman-Remote-Worker",
            "credentials": {
                "auth_method": "github_app",
                "app_id": "123456",
                "installation_id": "987654",
                "private_key": "-----BEGIN RSA PRIVATE KEY-----\nx\n-----END RSA PRIVATE KEY-----\n",
            },
            **extra,
        }

    @staticmethod
    def fake_token():
        """Stand-in for orchestrator.services.github_app.InstallationToken."""
        tok = MagicMock()
        tok.token = "ghs_secret"
        tok.expires_at = 9e12
        return tok

    def test_clone_url_is_clean_and_token_lands_in_store_file(self):
        ws = make_workspace_manager()
        git_mgr = MagicMock()
        # Patch the bridge module, NOT datasource_setup: the implementation
        # imports the name function-locally, so the lookup happens at call
        # time against this module's attribute.
        with (
            patch(
                "src.core.github_app_bridge.get_installation_token",
                return_value=self.fake_token(),
            ),
            patch(
                "src.managers.git_manager.GitManager.clone", return_value=git_mgr
            ) as mock_clone,
        ):
            clone_repository_datasources([self.app_ds()], ws)

        url_arg = mock_clone.call_args[0][0]
        assert "ghs_secret" not in url_arg
        assert url_arg == (
            "https://github.com/Knaeckebrothero/Superhuman-Remote-Worker.git"
        )

        # Secret went to the 0600 store file instead.
        written = {
            c.args[0]: c.args[1]
            for c in ws.backend.write_home_file.call_args_list
        }
        store_rel = next(p for p in written if "git-credentials" in p)
        assert "ghs_secret" in written[store_rel]
        assert store_rel.startswith(".config/srw/git-credentials/")

    def test_helper_and_usehttppath_are_persisted_on_the_repo(self):
        ws = make_workspace_manager()
        git_mgr = MagicMock()
        with (
            patch(
                "src.core.github_app_bridge.get_installation_token",
                return_value=self.fake_token(),
            ),
            patch("src.managers.git_manager.GitManager.clone", return_value=git_mgr),
        ):
            clone_repository_datasources([self.app_ds(default_branch="develop")], ws)

        config_calls = [c[0][0] for c in git_mgr._run_git.call_args_list]
        flat = [" ".join(args) for args in config_calls]
        assert any("credential.helper" in c and "store --file=" in c for c in flat)
        # useHttpPath keeps two github.com repos with different Apps from
        # colliding on a single github.com credential entry.
        assert any("credential.useHttpPath" in c for c in flat)
        git_mgr.checkout_branch.assert_called_once_with("develop")

    def test_store_file_is_chmod_600(self):
        ws = make_workspace_manager()
        with (
            patch(
                "src.core.github_app_bridge.get_installation_token",
                return_value=self.fake_token(),
            ),
            patch(
                "src.managers.git_manager.GitManager.clone", return_value=MagicMock()
            ),
        ):
            clone_repository_datasources([self.app_ds()], ws)

        shell_cmds = [c[0][0] for c in ws.backend.shell_run.call_args_list]
        assert any(
            "chmod 600" in cmd and "git-credentials" in cmd for cmd in shell_cmds
        )
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_datasource_repo_clone.py -v -k github_app`
Expected: FAIL — no `github_app` branch exists, so the datasource falls through to the unauthenticated path and no store file is written.

- [ ] **Step 3: Write the implementation**

In `src/core/datasource_setup.py`, add this helper immediately above `clone_repository_datasources`:

```python
def _write_git_credential_store(
    backend: Any, ds_slug: str, repo_url: str, token: str
) -> str:
    """Materialize a 0600 git-credentials file and return its absolute path.

    The file holds one line in git's credential-store format. Combined with
    ``credential.useHttpPath=true`` on the repo, git matches on the full path,
    so two datasources pointing at different GitHub repos (each with its own
    App) never collide on ``github.com``.
    """
    parsed = urlparse(repo_url)
    host = parsed.hostname or "github.com"
    path = parsed.path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]

    rel = f".config/srw/git-credentials/{ds_slug}"
    backend.shell_run(
        "mkdir -p ~/.config/srw/git-credentials && chmod 700 ~/.config/srw/git-credentials",
        timeout=10,
        tab_name="git",
    )
    backend.write_home_file(rel, f"https://x-access-token:{token}@{host}/{path}.git\n")
    abs_path = backend.resolve_home_path(rel)
    backend.shell_run(f"chmod 600 {shlex.quote(abs_path)}", timeout=10, tab_name="git")
    return abs_path
```

Add the import near the other local imports inside `clone_repository_datasources`:

```python
    from ..managers.git_manager import GitManager
    from ..utils.ssh_key import normalize_private_key
    from .github_app_bridge import get_installation_token
```

Create `src/core/github_app_bridge.py` — the agent process cannot import from
`orchestrator/`, so the minting call is re-exported through a thin seam:

```python
"""Access to GitHub App token minting from the agent process.

The agent and the orchestrator are separate images; ``orchestrator.services``
is not importable here. This module is the single seam, so if minting later
moves behind an orchestrator HTTP call only this file changes.
"""

import asyncio
from typing import Any


def get_installation_token(app_id: str, private_key_pem: str, installation_id: str) -> Any:
    """Mint (or reuse) an installation token, synchronously.

    ``clone_repository_datasources`` is sync and runs at dispatch, so the
    async minting call is driven to completion here.
    """
    from orchestrator.services.github_app import (  # noqa: PLC0415
        get_installation_token as _async_get,
    )

    return asyncio.run(_async_get(app_id, private_key_pem, installation_id))
```

> **Verify this import actually resolves in the agent image before relying on it.**
> Per `docs/features/orchestrator_image_missing_agent_deps.md` the two images do not
> share dependencies, and a cross-image import is exactly the class of bug that file
> records. Run the check in Step 4b. If it fails, stop and report — the fix is to
> mint in the orchestrator at dispatch and pass the token through the datasource
> dict, not to paper over the ImportError.

Then in `clone_repository_datasources`, add a branch **before** the existing
`elif (auth_method == "token" or not auth_method) and creds.get("token"):`:

```python
            elif auth_method == "github_app":
                minted = get_installation_token(
                    creds["app_id"], creds["private_key"], creds["installation_id"]
                )
                # Clean URL: the secret goes in the store file, never here.
                parsed = urlparse(repo_url)
                clean_path = parsed.path.strip("/")
                if not clean_path.endswith(".git"):
                    clean_path += ".git"
                repo_url = f"https://{parsed.hostname}/{clean_path}"
                store_path = _write_git_credential_store(
                    backend, ds_name, repo_url, minted.token
                )
                git_config_args = [
                    f"credential.helper=store --file={store_path}",
                    "credential.useHttpPath=true",
                ]
```

Initialize `git_config_args: List[str] = []` at the top of the per-datasource
`try:` block, and after a successful clone persist the helper onto the repo:

```python
            if git_mgr:
                for arg in git_config_args:
                    key, _, value = arg.partition("=")
                    git_mgr._run_git(["config", key, value])
                if branch:
                    git_mgr.checkout_branch(branch)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_datasource_repo_clone.py -v`
Expected: PASS, including the pre-existing ssh and token tests (coexistence).

- [ ] **Step 4b: Verify the cross-image import resolves**

Run the import inside a live agent pod — the only place with the agent image's real
dependency set:

```bash
kubectl --context=k3d-srw -n srw get pods -l app=srw-agent -o name | head -1
# then, using that pod name:
kubectl --context=k3d-srw -n srw exec <pod> -- \
  python3 -c "from src.core.github_app_bridge import get_installation_token; print('ok')"
```

Expected: `ok`.

**A failure here blocks the task — report it, do not work around it.** An
`ImportError` on `orchestrator.services.github_app` means the agent image cannot
reach orchestrator code at all, which invalidates the bridge design rather than
just this call. The fix is then to mint in the orchestrator at dispatch and pass
the token through the datasource dict into `clone_repository_datasources`, so the
agent never imports orchestrator code. That is a different Task 3 — stop and
re-plan it rather than adding the dependency to the agent image.

- [ ] **Step 5: Lint and commit**

```bash
ruff check src/core/datasource_setup.py src/core/github_app_bridge.py tests/test_datasource_repo_clone.py
ruff format src/core/datasource_setup.py src/core/github_app_bridge.py tests/test_datasource_repo_clone.py
git add src/core/datasource_setup.py src/core/github_app_bridge.py tests/test_datasource_repo_clone.py
git commit -m "feat(github-app): clone via credential store file, keep token out of .git/config"
```

---

### Task 4: Per-datasource commit identity

**Files:**
- Modify: `src/managers/git_manager.py:165-166, 834-835, 915-916, 965-966`
- Modify: `src/core/datasource_setup.py` (pass `commit_identity` through at clone)
- Test: `tests/test_managers_git.py`

**Interfaces:**
- Consumes: the cloned `GitManager` instances from Task 3.
- Produces:
  - Module constants `DEFAULT_AGENT_NAME = "Agent"`, `DEFAULT_AGENT_EMAIL = "agent@workspace.local"`
  - `GitManager._configure_identity(self, name: str | None = None, email: str | None = None) -> None`
  - Optional `credentials.commit_identity = {"name": str, "email": str}` on repository datasources

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_managers_git.py`:

```python
def test_configure_identity_defaults_preserve_workspace_behaviour(tmp_path):
    """The internal workspace repo must keep its historical identity."""
    from src.managers.git_manager import (
        DEFAULT_AGENT_EMAIL,
        DEFAULT_AGENT_NAME,
        GitManager,
    )

    mgr = GitManager(tmp_path)
    mgr.init_repository()

    assert mgr._run_git(["config", "user.name"]).strip() == DEFAULT_AGENT_NAME
    assert mgr._run_git(["config", "user.email"]).strip() == DEFAULT_AGENT_EMAIL


def test_configure_identity_applies_override(tmp_path):
    from src.managers.git_manager import GitManager

    mgr = GitManager(tmp_path)
    mgr.init_repository()
    mgr._configure_identity(name="srw-agent[bot]", email="bot@example.invalid")

    assert mgr._run_git(["config", "user.name"]).strip() == "srw-agent[bot]"
    assert mgr._run_git(["config", "user.email"]).strip() == "bot@example.invalid"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_managers_git.py -v -k identity`
Expected: FAIL — `ImportError: cannot import name 'DEFAULT_AGENT_NAME'`

- [ ] **Step 3: Write the implementation**

Near the top of `src/managers/git_manager.py`, after the existing module constants:

```python
# Historical identity for the INTERNAL workspace repo. External repository
# datasources override this per-datasource via commit_identity.
DEFAULT_AGENT_NAME = "Agent"
DEFAULT_AGENT_EMAIL = "agent@workspace.local"
```

Add the method to `GitManager`:

```python
    def _configure_identity(
        self, name: str | None = None, email: str | None = None
    ) -> None:
        """Set user.name / user.email on this repo.

        Cosmetic-but-real: GitHub renders the configured author on each commit.
        It is a label, not an attestation — anyone can set these strings.
        """
        self._run_git(["config", "user.name", name or DEFAULT_AGENT_NAME])
        self._run_git(["config", "user.email", email or DEFAULT_AGENT_EMAIL])
```

Replace each of the four hardcoded pairs (lines 165-166, 834-835, 915-916, 965-966) — each is a
`_run_git(["config", "user.email", "agent@workspace.local"])` followed by a
`user.name` line — with a single call. Inside `GitManager` methods use
`self._configure_identity()`; at the two `clone()` sites and `from_worktree()` the
instance is the local `mgr`, so use `mgr._configure_identity()`.

Then in `src/core/datasource_setup.py`, inside the `if git_mgr:` block from Task 3,
apply the datasource's identity before checking out the branch:

```python
                identity = (creds.get("commit_identity") or {}) if creds else {}
                git_mgr._configure_identity(
                    name=identity.get("name"), email=identity.get("email")
                )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_managers_git.py tests/test_phase_git.py tests/test_per_job_repo.py -v`
Expected: PASS. The extra suites guard against a missed identity site breaking workspace versioning.

- [ ] **Step 5: Lint and commit**

```bash
ruff check src/managers/git_manager.py src/core/datasource_setup.py tests/test_managers_git.py
ruff format src/managers/git_manager.py src/core/datasource_setup.py tests/test_managers_git.py
git add src/managers/git_manager.py src/core/datasource_setup.py tests/test_managers_git.py
git commit -m "feat(git): per-datasource commit identity, collapse four hardcoded sites"
```

---

## What this plan deliberately does not do

Recorded so the next plan does not re-derive them:

- **`repo_commit` / `repo_push` / `repo_pull` / `repo_open_pr` write tools.** They need their own `DATASOURCE_TOOL_MAP` **category** — adding a `repository` entry under the existing `git` category would strip the workspace git tools whenever no repo datasource is attached (`datasource_tool_categories`, `src/core/datasource_setup.py:155`). That is the first thing the next plan must get right.
- **Token re-mint before push.** Task 1's cache is process-local to whoever calls it; a job that outlives one token needs `repo_push` to refresh. Until that lands, a long job's push can fail on an expired token — the known gap this plan leaves open.
- **Cockpit UI + i18n** for the `github_app` auth method.
- **Skill update.** `docs/skills/repo-contribution/SKILL.md` still describes raw shell git.
- **Retiring the existing SRW Repository PAT.** Coexistence is deliberate; cut over once the App path is proven.

## Verification before calling this done

```bash
python -m pytest tests/test_github_app.py tests/test_github_app_datasource.py \
  tests/test_datasource_repo_clone.py tests/test_managers_git.py \
  tests/test_phase_git.py tests/test_per_job_repo.py tests/test_ssh_key_utils.py -v
ruff check src/ orchestrator/ tests/
```

Then the live gate on k3d, which is the only thing that proves the seam works:

1. Register a GitHub App on your account (permissions: `Contents: write`, `Pull requests: write`), install it on `Superhuman-Remote-Worker`, note App ID + Installation ID, download the private key.
2. Create a second repository datasource (do **not** modify the existing PAT-backed one) with `auth_method: github_app`.
3. Dispatch a job against it.
4. On the workspace: `git -C repos/Superhuman-Remote-Worker remote -v` must show a **clean** URL with no token, and `git -C repos/Superhuman-Remote-Worker config credential.helper` must point at the store file.
5. Confirm the store file is `0600` and gone after teardown.
6. Repeat 1–5 for **KurortEngine** (install the same App on it, add a second `github_app` datasource). It currently has `credentials={}` and cannot push at all, so it is the cleanest proof that the path works on a repo that has never had a working credential — and it is the second repo in scope for this rollout.
