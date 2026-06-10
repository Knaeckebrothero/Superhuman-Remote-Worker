"""Shared Keycloak bearer-token client for OpenCloud data planes.

Both the legacy WebDAV sync coordinator
(``src/services/cloud_sync/opencloud_sync.py``) and the rclone mount manager
(``src/services/cloud_mount/``) need the same token dance against the
deployment's Keycloak:

* mint a service-account token via ``client_credentials``;
* optionally exchange it for a user-scoped token via RFC 8693
  (``requested_subject`` impersonation — requires the realm ``impersonation``
  role on the calling client);
* cache the result with a clock-skew-aware expiry;
* allow a forced refresh when a data-plane call hits a 401.

The token endpoints intentionally mirror the orchestrator backend
(``orchestrator/services/cloud/opencloud.py``) — same ``openid`` scope so
OpenCloud's OIDC middleware can resolve userinfo for the bearer.

Never log token response bodies: error texts may echo the request payload,
which contains the client secret.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Optional

import httpx

_TOKEN_CLOCK_SKEW_SECONDS = 30.0


@dataclass(frozen=True)
class BearerToken:
    """An access token plus its (skew-adjusted) monotonic expiry deadline."""

    token: str
    expires_at: float

    @property
    def ttl_seconds(self) -> float:
        """Seconds until this token should be considered expired (>= 0)."""
        return max(0.0, self.expires_at - time.monotonic())


class KeycloakTokenClient:
    """Caching Keycloak token client (client_credentials + impersonation).

    With ``target_user_sub`` set, every refresh first mints a service token
    and then exchanges it for a user-scoped token whose ``sub`` claim is the
    target user. Without it, the service token itself is returned.
    """

    def __init__(
        self,
        *,
        issuer: str,
        client_id: str,
        client_secret: str,
        target_user_sub: Optional[str] = None,
        httpx_client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self._issuer = issuer.rstrip("/")
        self._client_id = client_id
        self._client_secret = client_secret
        self._target_user_sub = target_user_sub

        self._httpx: Optional[httpx.AsyncClient] = httpx_client
        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0.0
        self._token_lock = asyncio.Lock()

    def __repr__(self) -> str:
        mode = (
            f"impersonate_sub={self._target_user_sub}"
            if self._target_user_sub
            else "service-account"
        )
        return (
            "<KeycloakTokenClient "
            f"issuer={self._issuer} client_id={self._client_id} secret=*** {mode}>"
        )

    # --------------------------------------------------------------- Internals

    def _httpx_client(self) -> httpx.AsyncClient:
        if self._httpx is None:
            self._httpx = httpx.AsyncClient(timeout=30.0)
        return self._httpx

    async def _fetch_service_token(self) -> tuple[str, float]:
        """Mint a fresh service-account token via client_credentials.

        Returns ``(access_token, expires_in)``. Caller decides whether to
        cache it directly or feed it into a token-exchange.
        """
        client = self._httpx_client()
        resp = await client.post(
            f"{self._issuer}/protocol/openid-connect/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                # openid scope required so OpenCloud's OIDC middleware can
                # call Keycloak userinfo for the bearer.
                "scope": "openid",
            },
            headers={"Accept": "application/json"},
        )
        # Don't log resp.text on failure — it may echo the request body.
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
        service-account user. The calling client must hold the realm
        ``impersonation`` role for Keycloak to honor this.
        """
        client = self._httpx_client()
        resp = await client.post(
            f"{self._issuer}/protocol/openid-connect/token",
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "subject_token": subject_token,
                "subject_token_type": ("urn:ietf:params:oauth:token-type:access_token"),
                "requested_subject": target_sub,
                # openid scope keeps OpenCloud's OIDC middleware happy when
                # it validates the exchanged token.
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

    # ------------------------------------------------------------------- API

    async def get_bearer(self, force_refresh: bool = False) -> BearerToken:
        """Return the current bearer token with its expiry deadline.

        For service-account mode this is the client_credentials token. For
        impersonation mode it is the user-scoped token from the exchange —
        minted from a fresh service token on each refresh.
        """
        now = time.monotonic()
        if not force_refresh and self._access_token and now < self._token_expires_at:
            return BearerToken(self._access_token, self._token_expires_at)
        async with self._token_lock:
            now = time.monotonic()
            if (
                not force_refresh
                and self._access_token
                and now < self._token_expires_at
            ):
                return BearerToken(self._access_token, self._token_expires_at)

            service_token, service_ttl = await self._fetch_service_token()
            if self._target_user_sub:
                user_token, ttl = await self._exchange_for_user_token(
                    service_token, self._target_user_sub
                )
                self._access_token = user_token
            else:
                self._access_token = service_token
                ttl = service_ttl
            self._token_expires_at = time.monotonic() + ttl - _TOKEN_CLOCK_SKEW_SECONDS
            return BearerToken(self._access_token, self._token_expires_at)

    async def get_token(self, force_refresh: bool = False) -> str:
        """Return the current bearer token string, refreshing if needed."""
        return (await self.get_bearer(force_refresh=force_refresh)).token

    def invalidate(self) -> None:
        """Drop the cached token so the next ``get_*`` mints a fresh one."""
        self._access_token = None
        self._token_expires_at = 0.0

    async def aclose(self) -> None:
        """Drop cached token + secret + HTTP client."""
        self.invalidate()
        self._client_secret = ""
        if self._httpx is not None:
            try:
                await self._httpx.aclose()
            except Exception:
                pass
            self._httpx = None
