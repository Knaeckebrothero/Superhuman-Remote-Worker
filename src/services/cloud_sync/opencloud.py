"""OpenCloud workspace sync — WebDAV + Keycloak client_credentials bearer token.

The token is minted from Keycloak via the service account client_credentials
flow (mirrors ``orchestrator/services/cloud/opencloud.py:_get_service_token``),
cached in memory, and refreshed ~30s before expiry. The underlying webdav3
client is rebuilt each time the token rotates.

If a primitive gets a 401 mid-call (e.g. Keycloak rotated the signing key),
the wrapper clears the cached token, forces a refresh, and retries once. A
persistent 401 indicates a real config error and surfaces to the caller.
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
        poll_interval: int = 15,
        workspace_backend: Optional["WorkspaceBackend"] = None,
    ) -> None:
        super().__init__(
            workspace_path,
            poll_interval=poll_interval,
            workspace_backend=workspace_backend,
        )
        self._webdav_base_url = webdav_base_url.rstrip("/") + "/"
        self._keycloak_issuer = keycloak_issuer.rstrip("/")
        self._client_id = client_id
        self._client_secret = client_secret

        self._webdav_base_path = urlparse(self._webdav_base_url).path.rstrip("/") + "/"

        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0.0
        self._token_lock = asyncio.Lock()

        self._httpx: Optional[httpx.AsyncClient] = None
        self._dav_client = None
        self._current_client_token: Optional[str] = None

    def __repr__(self) -> str:
        return (
            "<OpenCloudWorkspaceSync "
            f"issuer={self._keycloak_issuer} "
            f"client_id={self._client_id} "
            "secret=*** "
            f"webdav={self._webdav_base_url}>"
        )

    # ----------------------------------------------------------- Token handling

    def _httpx_client(self) -> httpx.AsyncClient:
        if self._httpx is None:
            self._httpx = httpx.AsyncClient(timeout=30.0)
        return self._httpx

    async def _get_token(self, force_refresh: bool = False) -> str:
        """Return a cached or freshly-minted Keycloak service-account token."""
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
            self._access_token = str(token)
            self._token_expires_at = (
                time.monotonic() + float(expires_in) - _TOKEN_CLOCK_SKEW_SECONDS
            )
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
