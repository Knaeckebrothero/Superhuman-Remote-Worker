"""Thin Keycloak token-endpoint client for the cookie BFF.

Used by orchestrator/auth/bff.py to exchange an authorization code for tokens
(after the user comes back from KC) and to refresh the access token mid-session
when it's close to expiry. Not used for JWT validation — that's oidc.py.

Requires the cockpit Keycloak client to be set to **confidential** so this
module can authenticate at the /token endpoint with client_secret_basic.
The realm-config flip is part of the deployment work; the secret comes from
Vault/ESO via KC_CLIENT_SECRET.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class KeycloakClientError(Exception):
    """Raised when the KC token endpoint returns a non-2xx or malformed body."""


class KeycloakBFFClient:
    """OAuth code/refresh-grant client for the BFF.

    All endpoints derive from ``KEYCLOAK_URL`` + ``KEYCLOAK_REALM``; the
    redirect_uri comes from ``SRW_BFF_REDIRECT_URI``. ``KC_CLIENT_SECRET``
    is required at runtime — initialisation succeeds without it (so import
    doesn't crash in tests), but any call will fail loudly.
    """

    def __init__(self) -> None:
        self.keycloak_url = os.getenv("KEYCLOAK_URL", "http://localhost:8180")
        self.realm = os.getenv("KEYCLOAK_REALM", "srw")
        self.client_id = os.getenv("KEYCLOAK_CLIENT_ID", "cockpit")
        self.client_secret = os.getenv("KC_CLIENT_SECRET", "")
        self.redirect_uri = os.getenv("SRW_BFF_REDIRECT_URI", "")
        self.timeout = float(os.getenv("KC_HTTP_TIMEOUT_S", "10"))

    @property
    def token_endpoint(self) -> str:
        return f"{self.keycloak_url}/realms/{self.realm}/protocol/openid-connect/token"

    @property
    def authorize_endpoint(self) -> str:
        # KEYCLOAK_ISSUER_URL is the public-facing URL embedded in tokens; the
        # browser must hit /authorize via that hostname, not the internal
        # backchannel URL. Falls back to KEYCLOAK_URL for local dev.
        public_url = os.getenv("KEYCLOAK_ISSUER_URL", self.keycloak_url)
        return f"{public_url}/realms/{self.realm}/protocol/openid-connect/auth"

    @property
    def logout_endpoint(self) -> str:
        public_url = os.getenv("KEYCLOAK_ISSUER_URL", self.keycloak_url)
        return f"{public_url}/realms/{self.realm}/protocol/openid-connect/logout"

    @property
    def end_session_endpoint(self) -> str:
        # Same URL as logout_endpoint in current Keycloak (>= 19), but kept
        # as an alias because the OIDC spec name is "end_session_endpoint".
        return self.logout_endpoint

    def _basic_auth(self) -> tuple[str, str]:
        if not self.client_secret:
            raise KeycloakClientError(
                "KC_CLIENT_SECRET not configured — cannot authenticate at "
                "the Keycloak token endpoint. The cockpit client must be "
                "set to confidential in the realm config and the secret "
                "loaded into the orchestrator pod via Vault/ESO."
            )
        return (self.client_id, self.client_secret)

    async def exchange_code(self, code: str, pkce_verifier: str) -> dict[str, Any]:
        """Trade an authorization code for tokens.

        Returns the parsed token response dict on success:
            {access_token, refresh_token, id_token, expires_in, refresh_expires_in, ...}
        Raises KeycloakClientError on any non-2xx or malformed body.
        """
        if not self.redirect_uri:
            raise KeycloakClientError("SRW_BFF_REDIRECT_URI not configured")
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
            "code_verifier": pkce_verifier,
        }
        return await self._post_token(data)

    async def refresh(self, refresh_token: str) -> dict[str, Any]:
        """Use the stored refresh token to get a new access token.

        Returns the parsed token response. Raises KeycloakClientError on any
        non-2xx; the caller should treat that as "session is dead, kill it".
        """
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }
        return await self._post_token(data)

    async def logout_kc_side(self, refresh_token: str) -> None:
        """Best-effort revocation of the KC-side session via the refresh token.

        Called from /auth/logout before deleting the local session row. The
        front-channel redirect with id_token_hint is the primary mechanism;
        this call is belt-and-suspenders for users who close the tab before
        the redirect completes.
        """
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(
                    self.logout_endpoint,
                    auth=self._basic_auth(),
                    data={"refresh_token": refresh_token},
                )
            if resp.status_code >= 400:
                logger.warning(
                    "KC backchannel logout returned %d (best-effort; ignored)",
                    resp.status_code,
                )
        except (httpx.HTTPError, KeycloakClientError) as e:
            logger.warning("KC backchannel logout failed (best-effort; ignored): %s", e)

    async def _post_token(self, data: dict[str, str]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                resp = await client.post(
                    self.token_endpoint,
                    auth=self._basic_auth(),
                    data=data,
                    headers={"Accept": "application/json"},
                )
            except httpx.HTTPError as e:
                raise KeycloakClientError(f"KC token endpoint unreachable: {e}") from e

        if resp.status_code != 200:
            # Keycloak returns OAuth-style error bodies on 400/401. Log the
            # error code/description but never the request body (which may
            # contain a code/refresh_token in transit).
            try:
                body = resp.json()
                err = body.get("error", "unknown_error")
                desc = body.get("error_description", "")
            except ValueError:
                err = "non_json_response"
                desc = resp.text[:200]
            logger.warning(
                "KC token endpoint returned %d (%s): %s",
                resp.status_code,
                err,
                desc,
            )
            raise KeycloakClientError(f"KC token endpoint {resp.status_code} ({err})")

        try:
            return resp.json()
        except ValueError as e:
            raise KeycloakClientError(
                "KC token endpoint returned malformed JSON"
            ) from e


# Singleton — module-import side effects are bounded to env reads.
kc_bff_client = KeycloakBFFClient()
