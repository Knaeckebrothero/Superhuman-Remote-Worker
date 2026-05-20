"""OpenCloud workspace sync — WebDAV + Keycloak bearer token.

Two auth modes:

* **Service-account (default).** Token minted via Keycloak ``client_credentials``
  flow (mirrors ``orchestrator/services/cloud/opencloud.py:_get_service_token``).
  Used by Phase 1 project / repo mounts where the service account is invited
  to the Space and can read/write directly.

* **User impersonation** (set ``target_user_sub``). Token minted via RFC 8693
  token-exchange: first get the service-account token as before, then POST
  to the token endpoint with ``grant_type=urn:ietf:params:oauth:grant-type:
  token-exchange``, ``subject_token=<service token>``, ``requested_subject=
  <target sub>``. The exchanged token authenticates the agent as the target
  user. Required for Phase 2 user-home mounts (OpenCloud Personal Spaces are
  owned by exactly one user; the service account has no WebDAV access of its
  own to them).

In both modes the resulting bearer token is cached in memory, refreshed ~30s
before expiry, and the underlying webdav3 client is rebuilt when the token
rotates. If a primitive gets a 401 mid-call, the wrapper clears the cached
token, forces a fresh fetch (and re-exchange in impersonation mode), and
retries once. A persistent 401 surfaces as a real config error.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable, Optional, TypeVar
from urllib.parse import urlparse

import httpx

from .base import WorkspaceSyncBase

if TYPE_CHECKING:
    from ...core.workspace_backend import WorkspaceBackend

logger = logging.getLogger(__name__)

_TOKEN_CLOCK_SKEW_SECONDS = 30.0

T = TypeVar("T")


def _looks_like_401(exc: BaseException) -> bool:
    """Best-effort check for a 401 error surfaced by webdav3 or httpx."""
    code = getattr(exc, "code", None)
    if code == 401:
        return True
    status = getattr(exc, "status_code", None)
    if status == 401:
        return True
    return "401" in str(exc)


class OpenCloudWorkspaceSync(WorkspaceSyncBase):
    """OpenCloud sync client — bearer token via Keycloak client_credentials."""

    def __init__(
        self,
        workspace_path: Path,
        *,
        webdav_base_url: str,
        keycloak_issuer: str,
        client_id: str,
        client_secret: str,
        target_user_sub: Optional[str] = None,
        poll_interval: int = 15,
        workspace_backend: Optional["WorkspaceBackend"] = None,
        mount_subdir: str = "",
    ) -> None:
        super().__init__(
            workspace_path,
            poll_interval=poll_interval,
            workspace_backend=workspace_backend,
            mount_subdir=mount_subdir,
        )
        self._webdav_base_url = webdav_base_url.rstrip("/") + "/"
        self._keycloak_issuer = keycloak_issuer.rstrip("/")
        self._client_id = client_id
        self._client_secret = client_secret
        # When set, ``_get_token`` returns a user-scoped token obtained by
        # exchanging the service-account token for an impersonation token
        # naming this Keycloak ``sub``. None = legacy service-account mode.
        self._target_user_sub = target_user_sub

        self._webdav_base_path = urlparse(self._webdav_base_url).path.rstrip("/") + "/"

        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0.0
        self._token_lock = asyncio.Lock()

        self._httpx: Optional[httpx.AsyncClient] = None
        self._dav_client = None
        self._current_client_token: Optional[str] = None

    def __repr__(self) -> str:
        mode = (
            f"impersonate_sub={self._target_user_sub}"
            if self._target_user_sub
            else "service-account"
        )
        return (
            "<OpenCloudWorkspaceSync "
            f"issuer={self._keycloak_issuer} "
            f"client_id={self._client_id} "
            "secret=*** "
            f"{mode} "
            f"webdav={self._webdav_base_url}>"
        )

    # ----------------------------------------------------------- Token handling

    def _httpx_client(self) -> httpx.AsyncClient:
        if self._httpx is None:
            self._httpx = httpx.AsyncClient(timeout=30.0)
        return self._httpx

    async def _fetch_service_token(self) -> tuple[str, float]:
        """Mint a fresh service-account token via client_credentials.

        Returns ``(access_token, expires_in)``. Caller decides whether to
        cache it (legacy mode) or feed it into a token-exchange (impersonation
        mode).
        """
        client = self._httpx_client()
        resp = await client.post(
            f"{self._keycloak_issuer}/protocol/openid-connect/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                # openid scope required so OpenCloud's OIDC middleware
                # can call Keycloak userinfo. Mirrors the orchestrator
                # backend (orchestrator/services/cloud/opencloud.py).
                "scope": "openid",
            },
            headers={"Accept": "application/json"},
        )
        # Don't log resp.text on failure — it may echo request body.
        resp.raise_for_status()
        payload = resp.json()
        token = payload.get("access_token")
        expires_in = payload.get("expires_in")
        if not token or not isinstance(expires_in, (int, float)):
            raise RuntimeError(
                "Keycloak token response missing access_token/expires_in"
            )
        return str(token), float(expires_in)

    async def _exchange_for_user_token(
        self, subject_token: str, target_sub: str
    ) -> tuple[str, float]:
        """Exchange the service token for a user-scoped one via RFC 8693.

        Keycloak's ``requested_subject`` parameter triggers impersonation:
        the issued token's ``sub`` claim is ``target_sub`` instead of the
        service-account user. The calling client (us) must hold the realm
        ``impersonation`` role for Keycloak to honor this.
        """
        client = self._httpx_client()
        resp = await client.post(
            f"{self._keycloak_issuer}/protocol/openid-connect/token",
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "subject_token": subject_token,
                "subject_token_type": ("urn:ietf:params:oauth:token-type:access_token"),
                "requested_subject": target_sub,
                # openid scope keeps OpenCloud's OIDC middleware happy
                # when it validates the exchanged token; same as the
                # client_credentials path above.
                "scope": "openid",
            },
            headers={"Accept": "application/json"},
        )
        resp.raise_for_status()
        payload = resp.json()
        token = payload.get("access_token")
        expires_in = payload.get("expires_in")
        if not token or not isinstance(expires_in, (int, float)):
            raise RuntimeError(
                "Keycloak token-exchange response missing access_token/expires_in"
            )
        return str(token), float(expires_in)

    async def _get_token(self, force_refresh: bool = False) -> str:
        """Return the current bearer token, refreshing if needed.

        For service-account mode this is just the client_credentials token.
        For impersonation mode it's the user-scoped token from the
        token-exchange — minted from a fresh service token each refresh.
        """
        now = time.monotonic()
        if not force_refresh and self._access_token and now < self._token_expires_at:
            return self._access_token
        async with self._token_lock:
            now = time.monotonic()
            if (
                not force_refresh
                and self._access_token
                and now < self._token_expires_at
            ):
                return self._access_token

            service_token, service_ttl = await self._fetch_service_token()
            if self._target_user_sub:
                user_token, user_ttl = await self._exchange_for_user_token(
                    service_token, self._target_user_sub
                )
                self._access_token = user_token
                ttl = user_ttl
            else:
                self._access_token = service_token
                ttl = service_ttl
            self._token_expires_at = time.monotonic() + ttl - _TOKEN_CLOCK_SKEW_SECONDS
            return self._access_token

    async def _dav(self):
        """Return a webdav3 client bound to the current bearer token."""
        token = await self._get_token()
        if self._dav_client is None or self._current_client_token != token:
            from webdav3.client import Client

            self._dav_client = Client(
                {
                    "webdav_hostname": self._webdav_base_url.rstrip("/"),
                    "webdav_token": token,
                }
            )
            self._current_client_token = token
        return self._dav_client

    async def _with_401_retry(self, op: Callable[[], Awaitable[T]]) -> T:
        """Run ``op`` once; if 401, force a token refresh and retry once."""
        try:
            return await op()
        except Exception as e:
            if not _looks_like_401(e):
                raise
            logger.info(
                "OpenCloud WebDAV 401 — forcing token refresh and retrying once"
            )
            self._access_token = None
            self._token_expires_at = 0.0
            self._dav_client = None
            self._current_client_token = None
            await self._get_token(force_refresh=True)
            return await op()

    # ---------------------------------------------------------------- Primitives

    async def _ensure_ready(self) -> None:
        await self._dav()

    async def _ensure_remote_dir(self, rel_dir: str) -> None:
        async def _run():
            client = await self._dav()
            return await asyncio.to_thread(client.mkdir, rel_dir)

        await self._with_401_retry(_run)

    async def _upload_file(self, rel_path: str, local_path: str) -> None:
        async def _run():
            client = await self._dav()
            return await asyncio.to_thread(
                client.upload_sync,
                remote_path=rel_path,
                local_path=local_path,
            )

        await self._with_401_retry(_run)

    async def _list_remote_files(self) -> list[dict]:
        async def _run():
            client = await self._dav()
            return await asyncio.to_thread(client.list, "/", get_info=True)

        raw = await self._with_401_retry(_run)
        out: list[dict] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            raw_path = item.get("path", "")
            if raw_path.startswith(self._webdav_base_path):
                rel = raw_path[len(self._webdav_base_path) :]
            else:
                rel = raw_path.strip("/")
            out.append(
                {
                    "path": rel,
                    "etag": item.get("etag", "") or "",
                    "isdir": bool(item.get("isdir")),
                }
            )
        return out

    async def _download_file(self, rel_path: str, local_path: str) -> None:
        async def _run():
            client = await self._dav()
            return await asyncio.to_thread(
                client.download_sync,
                remote_path=rel_path,
                local_path=local_path,
            )

        await self._with_401_retry(_run)

    async def aclose(self) -> None:
        """Drop cached token + secret + HTTP clients."""
        self._access_token = None
        self._token_expires_at = 0.0
        self._client_secret = ""
        self._dav_client = None
        self._current_client_token = None
        if self._httpx is not None:
            try:
                await self._httpx.aclose()
            except Exception:
                pass
            self._httpx = None
