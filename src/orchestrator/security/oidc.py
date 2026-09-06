"""Keycloak OIDC token validation.

Validates access tokens from Keycloak using JWKS (RS256).
JWKS keys are cached and refreshed automatically by PyJWT's PyJWKClient.
"""

import logging
import os
from typing import Any

import jwt

logger = logging.getLogger(__name__)

# OIDC back-channel logout — `events` claim must contain this exact key.
# Defined by the OpenID Connect Back-Channel Logout spec § 2.4.
BACKCHANNEL_LOGOUT_EVENT = "http://schemas.openid.net/event/backchannel-logout"


class OIDCValidator:
    """Validates Keycloak access tokens via JWKS."""

    def __init__(self) -> None:
        self.keycloak_url = os.getenv("KEYCLOAK_URL", "http://localhost:8180")
        self.realm = os.getenv("KEYCLOAK_REALM", "srw")
        # Public-facing URL for token issuer validation — KC_HOSTNAME sets this
        # in tokens. Falls back to KEYCLOAK_URL for local dev where internal = external.
        self._issuer_url = os.getenv("KEYCLOAK_ISSUER_URL", self.keycloak_url)
        self._jwks_client: jwt.PyJWKClient | None = None

    @property
    def issuer(self) -> str:
        return f"{self._issuer_url}/realms/{self.realm}"

    @property
    def is_configured(self) -> bool:
        return bool(self.keycloak_url and self.realm)

    @property
    def jwks_client(self) -> jwt.PyJWKClient:
        if self._jwks_client is None:
            # Fetch JWKS via internal URL (backchannel, fast)
            jwks_uri = (
                f"{self.keycloak_url}/realms/{self.realm}/protocol/openid-connect/certs"
            )
            self._jwks_client = jwt.PyJWKClient(jwks_uri, cache_keys=True)
        return self._jwks_client

    def validate_token(self, token: str) -> dict[str, Any] | None:
        """Validate a Bearer token and return decoded claims.

        Returns None if validation fails (expired, invalid signature, etc.).
        Claims include: sub, email, preferred_username, realm_access, etc.
        """
        try:
            signing_key = self.jwks_client.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                issuer=self.issuer,
                options={
                    "verify_exp": True,
                    "verify_aud": False,  # Keycloak audience varies by client
                },
            )
            return claims
        except jwt.ExpiredSignatureError:
            logger.debug("OIDC token expired")
            return None
        except jwt.InvalidTokenError as e:
            logger.debug("OIDC token validation failed: %s", e)
            return None

    def decode_id_token(self, id_token: str) -> dict[str, Any] | None:
        """Verify and decode an id_token from the code-exchange response.

        Same JWKS path as access tokens — KC signs both with the realm key.
        Audience verification is left off (same rationale as validate_token:
        Keycloak's audience claim varies with client + token-mapper config,
        and we already know the token came from a successful BFF exchange).
        Returns None on signature/expiry failure.
        """
        try:
            signing_key = self.jwks_client.get_signing_key_from_jwt(id_token)
            return jwt.decode(
                id_token,
                signing_key.key,
                algorithms=["RS256"],
                issuer=self.issuer,
                options={"verify_exp": True, "verify_aud": False},
            )
        except jwt.ExpiredSignatureError:
            logger.debug("OIDC id_token expired")
            return None
        except jwt.InvalidTokenError as e:
            logger.debug("OIDC id_token validation failed: %s", e)
            return None

    def verify_logout_token(self, logout_token: str) -> dict[str, Any] | None:
        """Verify a back-channel logout token per OIDC BCL §2.6.

        Keycloak POSTs this token to /auth/backchannel-logout when a user
        logs out at the IdP. We MUST verify signature + issuer + that the
        ``events`` claim contains the backchannel-logout key + that a
        subject identifier (sub or sid) is present + that ``nonce`` is
        absent (spec § 2.4).

        Returns the claims dict on success, None on any failure (treat as
        "ignore this request" — we don't want to leak which sids exist).
        """
        try:
            signing_key = self.jwks_client.get_signing_key_from_jwt(logout_token)
            claims = jwt.decode(
                logout_token,
                signing_key.key,
                algorithms=["RS256"],
                issuer=self.issuer,
                options={
                    "verify_exp": True,
                    "verify_aud": False,
                },
            )
        except jwt.InvalidTokenError as e:
            logger.warning("Back-channel logout token validation failed: %s", e)
            return None

        # Spec-required structural checks beyond what PyJWT enforces:
        events = claims.get("events") or {}
        if not isinstance(events, dict) or BACKCHANNEL_LOGOUT_EVENT not in events:
            logger.warning("Back-channel logout token missing required event claim")
            return None
        if "nonce" in claims:
            logger.warning("Back-channel logout token must not carry nonce")
            return None
        if not claims.get("sub") and not claims.get("sid"):
            logger.warning("Back-channel logout token has neither sub nor sid")
            return None
        return claims


# Singleton instance
oidc_validator = OIDCValidator()
