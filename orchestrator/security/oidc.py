"""Keycloak OIDC token validation.

Validates access tokens from Keycloak using JWKS (RS256).
JWKS keys are cached and refreshed automatically by PyJWT's PyJWKClient.
"""

import logging
import os
from typing import Any

import jwt

logger = logging.getLogger(__name__)


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


# Singleton instance
oidc_validator = OIDCValidator()
