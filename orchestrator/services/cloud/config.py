"""Pydantic settings models for main-cloud backends.

Defines a discriminated union over the per-backend settings classes so that
a single ``load_main_cloud_config()`` call can validate deploy-time env vars
against the right schema for whichever backend is selected.

Phase 1.5 lands the ``NextcloudSettings`` / ``OpenCloudSettings`` /
``MS365Settings`` classes and the loader; only ``NextcloudSettings`` is
actually consumed by ``NextcloudBackend`` today — the other two exist so
Phase 2 (OpenCloud adapter) and Phase 5 (MS365 adapter) can start from a
populated settings class instead of re-inventing env-var plumbing.

See §4.2 of ``docs/features/main_cloud_abstraction.md``.
"""

from __future__ import annotations

import os
from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, SecretStr


class NextcloudSettings(BaseModel):
    """Env-var-loaded settings for ``NextcloudBackend``.

    Legacy ``NEXTCLOUD_*`` env vars map onto these fields (see
    ``load_main_cloud_config`` for the alias table).
    """

    model_config = ConfigDict(extra="forbid")

    backend_id: Literal["nextcloud"] = "nextcloud"
    base_url: HttpUrl
    public_url: HttpUrl
    admin_user: str
    admin_password: SecretStr
    agent_user: str
    agent_password: SecretStr
    oidc_client_secret: Optional[SecretStr] = None


class OpenCloudSettings(BaseModel):
    """Env-var-loaded settings for the (upcoming) OpenCloud adapter."""

    model_config = ConfigDict(extra="forbid")

    backend_id: Literal["opencloud"] = "opencloud"
    base_url: HttpUrl
    public_url: HttpUrl
    keycloak_issuer: HttpUrl
    keycloak_client_id: str
    keycloak_client_secret: SecretStr
    # Must match OpenCloud's built-in role assignment claim values. The
    # proxy driver's role_mapping is not configurable via env vars, so
    # the Keycloak group name has to line up with OpenCloud's defaults
    # (`opencloudAdmin`, `opencloudSpaceadmin`, `opencloudUser`,
    # `opencloudGuest`). Override only if you've mounted a custom
    # proxy.yaml with a different role_mapping.
    admin_role_claim_value: str = "opencloudAdmin"
    default_quota_bytes: Optional[int] = None


class MS365Settings(BaseModel):
    """Env-var-loaded settings for the (future) Microsoft 365 adapter."""

    model_config = ConfigDict(extra="forbid")

    backend_id: Literal["ms365"] = "ms365"
    tenant_id: str
    client_id: str
    client_secret: SecretStr
    site_id: Optional[str] = None


MainCloudConfig = Annotated[
    Union[NextcloudSettings, OpenCloudSettings, MS365Settings],
    Field(discriminator="backend_id"),
]


class _MainCloudConfigHolder(BaseModel):
    """Internal wrapper so Pydantic resolves the discriminated union."""

    model_config = ConfigDict(extra="forbid")

    settings: MainCloudConfig


def _detect_legacy_nextcloud_mode() -> bool:
    """Return True if the env looks like a pre-abstraction Nextcloud deploy.

    Heuristic: no ``MAIN_CLOUD_BACKEND`` set AND at least one ``NEXTCLOUD_*``
    var is set (beyond the port override). Phase 3 uses this to keep
    existing Nextcloud deployments on Nextcloud after the greenfield
    default flipped to OpenCloud — operators upgrading in place do not
    have to add ``MAIN_CLOUD_BACKEND=nextcloud`` to their ``.env``
    unless they explicitly want to migrate.

    ``NEXTCLOUD_PORT`` alone does not count as "legacy" since it may be
    set by the compose stack even on an OpenCloud-primary deployment
    that still keeps the Nextcloud profile available.
    """
    if os.getenv("MAIN_CLOUD_BACKEND"):
        return False
    for key in os.environ:
        if not key.startswith("NEXTCLOUD_"):
            continue
        if key == "NEXTCLOUD_PORT":
            continue
        return True
    return False


def _pick(*names: str, default: Optional[str] = None) -> Optional[str]:
    """First non-empty env var from ``names``, else ``default``."""
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return default


def load_main_cloud_config(
    *,
    backend_override: Optional[str] = None,
    db_overlay: Optional[dict] = None,
) -> MainCloudConfig:
    """Load and validate the deploy-time main-cloud config.

    Resolution order for the backend id:

    1. ``backend_override`` parameter (used by ``MainCloudRouter`` to
       instantiate a cached legacy backend for non-destructive switching).
    2. ``db_overlay["value"]["backend_id"]`` — the persisted setting
       from the cockpit admin UI (Phase 4).
    3. ``MAIN_CLOUD_BACKEND`` env var — operator override in .env.
    4. ``_detect_legacy_nextcloud_mode()`` heuristic.
    5. Default → ``opencloud`` (Phase 3 greenfield).

    Each non-secret field is resolved as ``db_overlay.value`` > env var >
    hardcoded default. Secret fields (passwords, client secrets) always
    come from the env var named in ``db_overlay.credentials_ref`` — if
    ``credentials_ref`` is ``None`` or doesn't start with ``env:``, the
    reader falls back to the legacy env var for that field directly.

    Parameters
    ----------
    backend_override:
        Force a specific backend id.
    db_overlay:
        A dict with keys ``value`` (dict) and ``credentials_ref`` (str or
        None), matching the shape returned by
        ``PostgresDB.get_system_setting('main_cloud')``.

    Raises
    ------
    pydantic.ValidationError:
        If required env vars / overlay fields are missing or invalid.
    ValueError:
        If ``backend_id`` is unknown.
    """
    overlay_value: dict = {}
    credentials_ref: Optional[str] = None
    if db_overlay:
        raw_value = db_overlay.get("value") or {}
        if isinstance(raw_value, dict):
            overlay_value = raw_value
        credentials_ref = db_overlay.get("credentials_ref")

    def _ov(key: str) -> Optional[str]:
        """Overlay lookup — returns None when the key is missing or empty."""
        val = overlay_value.get(key)
        if val is None or val == "":
            return None
        return str(val)

    def _secret(field: str, *env_fallbacks: str) -> Optional[str]:
        """Resolve a secret field via credentials_ref or env fallback.

        If ``credentials_ref`` is ``"env:NAME"`` and ``overlay_value``
        marks this field as credentials-ref-sourced (by listing it in
        ``__secret_fields__``), read ``os.getenv("NAME")``. Otherwise
        fall back to the first non-empty env var from ``env_fallbacks``.
        """
        secret_fields = overlay_value.get("__secret_fields__", [])
        if (
            credentials_ref
            and credentials_ref.startswith("env:")
            and field in secret_fields
        ):
            return os.getenv(credentials_ref[4:]) or None
        return _pick(*env_fallbacks)

    backend_id = (
        backend_override
        or overlay_value.get("backend_id")
        or os.getenv("MAIN_CLOUD_BACKEND")
    )
    if not backend_id:
        # Phase 3: the greenfield default is OpenCloud. Deployments that
        # already have NEXTCLOUD_* env vars set (i.e. in-place upgrades
        # from Phase 1/2) keep running on Nextcloud via the legacy
        # heuristic, so nobody gets silently migrated by the upgrade.
        backend_id = "nextcloud" if _detect_legacy_nextcloud_mode() else "opencloud"

    if backend_id == "nextcloud":
        base_url = _ov("base_url") or _pick(
            "MAIN_CLOUD_URL", "NEXTCLOUD_URL", default="http://localhost:8800"
        )
        public_url = _ov("public_url") or _pick(
            "MAIN_CLOUD_PUBLIC_URL", "NEXTCLOUD_PUBLIC_URL", default=base_url
        )
        raw = {
            "settings": {
                "backend_id": "nextcloud",
                "base_url": base_url,
                "public_url": public_url,
                "admin_user": _ov("admin_user")
                or _pick(
                    "MAIN_CLOUD_ADMIN_USER", "NEXTCLOUD_ADMIN_USER", default="admin"
                ),
                "admin_password": _secret(
                    "admin_password",
                    "MAIN_CLOUD_ADMIN_PASSWORD",
                    "NEXTCLOUD_ADMIN_PASSWORD",
                )
                or "admin",
                "agent_user": _ov("agent_user")
                or _pick(
                    "MAIN_CLOUD_AGENT_USER",
                    "NEXTCLOUD_AGENT_USER",
                    default="agent-service",
                ),
                "agent_password": _secret(
                    "agent_password",
                    "MAIN_CLOUD_AGENT_PASSWORD",
                    "NEXTCLOUD_AGENT_PASSWORD",
                )
                or "agent-service-dev",
                "oidc_client_secret": _secret(
                    "oidc_client_secret", "NEXTCLOUD_OIDC_CLIENT_SECRET"
                ),
            }
        }
    elif backend_id == "opencloud":
        # Defaults match the bundled docker-compose dev stack so a fresh
        # .env "just works" out of the box. Production deployments override
        # via Helm secrets / Vault — same convention as the Nextcloud branch
        # above. The client_secret default matches the value created by
        # docker/keycloak/setup-opencloud-client.sh.
        oc_base_url = _ov("base_url") or _pick(
            "MAIN_CLOUD_URL", "OPENCLOUD_URL", default="http://localhost:9200"
        )
        oc_public_url = _ov("public_url") or _pick(
            "MAIN_CLOUD_PUBLIC_URL", "OPENCLOUD_PUBLIC_URL", default=oc_base_url
        )
        raw = {
            "settings": {
                "backend_id": "opencloud",
                "base_url": oc_base_url,
                "public_url": oc_public_url,
                "keycloak_issuer": _ov("keycloak_issuer")
                or _pick(
                    "OPENCLOUD_KEYCLOAK_ISSUER",
                    default="http://localhost:8180/realms/srw",
                ),
                "keycloak_client_id": _ov("keycloak_client_id")
                or os.getenv(
                    "OPENCLOUD_KEYCLOAK_CLIENT_ID",
                    "opencloud-orchestrator",
                ),
                "keycloak_client_secret": _secret(
                    "keycloak_client_secret", "OPENCLOUD_KEYCLOAK_CLIENT_SECRET"
                )
                or "opencloud-orchestrator-local-secret",
                "admin_role_claim_value": _ov("admin_role_claim_value")
                or os.getenv("OPENCLOUD_ADMIN_ROLE_CLAIM_VALUE", "opencloudAdmin"),
                "default_quota_bytes": overlay_value.get("default_quota_bytes")
                if overlay_value.get("default_quota_bytes") is not None
                else _parse_int(os.getenv("OPENCLOUD_DEFAULT_QUOTA_BYTES")),
            }
        }
    elif backend_id == "ms365":
        raw = {
            "settings": {
                "backend_id": "ms365",
                "tenant_id": _ov("tenant_id") or os.getenv("MS365_TENANT_ID"),
                "client_id": _ov("client_id") or os.getenv("MS365_CLIENT_ID"),
                "client_secret": _secret("client_secret", "MS365_CLIENT_SECRET"),
                "site_id": _ov("site_id") or os.getenv("MS365_SITE_ID"),
            }
        }
    else:
        raise ValueError(
            f"unknown main cloud backend: {backend_id!r} "
            "(known: nextcloud, opencloud, ms365)"
        )

    holder = _MainCloudConfigHolder.model_validate(raw)
    return holder.settings


def _parse_int(value: Optional[str]) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except ValueError:
        return None


# Required (non-optional) secret fields per backend → the ordered env-var
# fallbacks ``load_main_cloud_config`` reads for each. Keep in lockstep with
# the ``_secret(...)`` calls in the loader above. ``oidc_client_secret`` is
# intentionally absent — it is ``Optional`` on ``NextcloudSettings``.
_REQUIRED_SECRET_ENVS: dict[str, dict[str, tuple[str, ...]]] = {
    "nextcloud": {
        "admin_password": ("MAIN_CLOUD_ADMIN_PASSWORD", "NEXTCLOUD_ADMIN_PASSWORD"),
        "agent_password": ("MAIN_CLOUD_AGENT_PASSWORD", "NEXTCLOUD_AGENT_PASSWORD"),
    },
    "opencloud": {
        "keycloak_client_secret": ("OPENCLOUD_KEYCLOAK_CLIENT_SECRET",),
    },
    "ms365": {
        "client_secret": ("MS365_CLIENT_SECRET",),
    },
}


def missing_secret_envs(
    backend_id: str, db_overlay: Optional[dict] = None
) -> list[dict]:
    """Report which *required* secret env vars are unset for a backend config.

    Mirrors the secret-resolution precedence of ``load_main_cloud_config``
    (``credentials_ref`` override > legacy env-var fallbacks) but only inspects
    *presence* — it never reads a secret's value and never falls back to the
    built-in dev defaults the loader uses (``admin`` / ``agent-service-dev`` /
    ``opencloud-orchestrator-local-secret``).

    The loader keeps those dev defaults on purpose so a bare ``.env`` or a test
    run "just works". This helper lets the admin endpoints **refuse to activate**
    (PUT) or **warn before probing** (test) a backend whose real secrets are not
    wired, instead of silently connecting with dev credentials and failing at
    the first cloud call. See ``docs/issues/main_cloud.md`` Issue 5.

    Returns one ``{"field", "env_var", "checked"}`` entry per missing secret; an
    empty list means every required secret resolves to a non-empty value.
    """
    required = _REQUIRED_SECRET_ENVS.get(backend_id, {})
    if not required:
        return []

    credentials_ref: Optional[str] = None
    secret_fields: list[str] = []
    if db_overlay:
        credentials_ref = db_overlay.get("credentials_ref")
        raw_value = db_overlay.get("value")
        if isinstance(raw_value, dict):
            secret_fields = raw_value.get("__secret_fields__", []) or []

    missing: list[dict] = []
    for field, env_fallbacks in required.items():
        # credentials_ref ("env:NAME") wins when the overlay marks this field
        # as credentials-ref-sourced — identical precedence to ``_secret()``.
        if (
            credentials_ref
            and credentials_ref.startswith("env:")
            and field in secret_fields
        ):
            env_name = credentials_ref[4:]
            if not os.getenv(env_name):
                missing.append(
                    {"field": field, "env_var": env_name, "checked": [env_name]}
                )
            continue
        if not _pick(*env_fallbacks):
            missing.append(
                {
                    "field": field,
                    "env_var": env_fallbacks[-1],
                    "checked": list(env_fallbacks),
                }
            )
    return missing
