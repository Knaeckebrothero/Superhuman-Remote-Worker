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
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable, Optional, TypeVar
from urllib.parse import urlparse

from ..keycloak_token import KeycloakTokenClient
from .base import WorkspaceSyncBase

if TYPE_CHECKING:
    from ...core.workspace_backend import WorkspaceBackend

logger = logging.getLogger(__name__)

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

        # Shared with the rclone mount manager — one implementation of the
        # client_credentials + token-exchange dance.
        self._token_client = KeycloakTokenClient(
            issuer=self._keycloak_issuer,
            client_id=client_id,
            client_secret=client_secret,
            target_user_sub=target_user_sub,
        )

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
    #
    # The actual Keycloak dance lives in ``src/services/keycloak_token.py``
    # (shared with the rclone mount manager). The shims below keep the
    # pre-extraction surface — tests and the 401-retry path reach into
    # ``_httpx`` / ``_access_token`` / ``_token_expires_at`` directly.

    @property
    def _httpx(self):
        return self._token_client._httpx

    @_httpx.setter
    def _httpx(self, value) -> None:
        self._token_client._httpx = value

    @property
    def _access_token(self) -> Optional[str]:
        return self._token_client._access_token

    @_access_token.setter
    def _access_token(self, value: Optional[str]) -> None:
        self._token_client._access_token = value

    @property
    def _token_expires_at(self) -> float:
        return self._token_client._token_expires_at

    @_token_expires_at.setter
    def _token_expires_at(self, value: float) -> None:
        self._token_client._token_expires_at = value

    async def _get_token(self, force_refresh: bool = False) -> str:
        """Return the current bearer token, refreshing if needed."""
        return await self._token_client.get_token(force_refresh=force_refresh)

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
        self._client_secret = ""
        self._dav_client = None
        self._current_client_token = None
        await self._token_client.aclose()
