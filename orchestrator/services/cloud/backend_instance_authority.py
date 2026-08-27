"""Pure authority for one immutable main-cloud backend installation.

Provider names (``nextcloud`` / ``opencloud``) are not routing authority: two
independent installations use the same provider-local folder, user, and group
identifier formats.  This module gives each configured installation a durable
UUID and binds it to an immutable, non-secret routing snapshot plus a digest of
an installation-specific remote proof.  Secret *references* may rotate under
an explicit revision; secret values never enter this object.

The database owns activation, reference tracking, and garbage collection.
Constructing or parsing this object performs no I/O and does not make an
installation active.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import SplitResult, urlsplit, urlunsplit
from uuid import UUID


MAIN_CLOUD_BACKEND_INSTANCE_VERSION = 1
MAIN_CLOUD_INSTALLATION_PROOF_DOMAIN = b"srw-main-cloud-installation-v1\0"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ENV_SECRET_REF_RE = re.compile(r"^env:[A-Za-z_][A-Za-z0-9_]*$")
_INSTANCE_BINDING_KEYS = frozenset(
    {
        "version",
        "backend_instance_id",
        "backend_id",
        "routing",
        "routing_sha256",
        "installation_proof_sha256",
        "secret_refs",
        "secret_revision",
    }
)
_NEXTCLOUD_ROUTING_KEYS = frozenset(
    {
        "version",
        "backend_id",
        "base_url",
        "public_url",
        "admin_user",
        "agent_user",
        "protected_effect_url",
        "protected_effect_config_sha256",
    }
)
_OPENCLOUD_ROUTING_KEYS = frozenset(
    {
        "version",
        "backend_id",
        "base_url",
        "public_url",
        "keycloak_issuer",
        "keycloak_client_id",
        "admin_role_claim_value",
        "default_quota_bytes",
        "mount_insecure_tls",
    }
)
_SECRET_FIELDS = {
    "nextcloud": frozenset({"admin_password", "agent_password"}),
    "opencloud": frozenset({"keycloak_client_secret"}),
}
_OPTIONAL_SECRET_FIELDS = {
    "nextcloud": frozenset({"oidc_client_secret", "protected_effect_hmac_key"}),
    "opencloud": frozenset(),
}
_INSTANCE_MARKER = object()


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _canonical_uuid(value: Any, *, coordinate: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"main-cloud {coordinate} is not a canonical UUID")
    try:
        parsed = UUID(value)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"main-cloud {coordinate} is not a canonical UUID") from exc
    canonical = str(parsed)
    if not parsed.int or value != canonical:
        raise ValueError(f"main-cloud {coordinate} is not a canonical UUID")
    return canonical


def _required_text(value: Any, *, coordinate: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or "\0" in value
        or any(ord(character) < 0x20 for character in value)
    ):
        raise ValueError(f"main-cloud {coordinate} is missing or malformed")
    return value


def _canonical_url(value: Any, *, coordinate: str) -> str:
    raw = _required_text(value, coordinate=coordinate)
    try:
        parsed = urlsplit(raw)
        # Accessing ``port`` performs urllib's invalid/range validation.
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise ValueError(f"main-cloud {coordinate} is not a safe URL") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"main-cloud {coordinate} is not a safe URL")
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    netloc = host if port is None else f"{host}:{port}"
    path = parsed.path.rstrip("/")
    parts = path.split("/")
    if any(part in {".", ".."} for part in parts):
        raise ValueError(f"main-cloud {coordinate} has an unsafe path")
    canonical = urlunsplit(SplitResult(parsed.scheme.lower(), netloc, path, "", ""))
    # Fresh settings may carry the harmless trailing slash Pydantic emits;
    # every persisted object is nevertheless one exact representation.
    return canonical


def _canonical_backend_id(value: Any) -> str:
    if not isinstance(value, str) or value not in _SECRET_FIELDS:
        raise ValueError("main-cloud backend is unsupported")
    return value


def _canonical_routing(
    backend_id: str,
    routing: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(routing, Mapping):
        raise ValueError("main-cloud routing snapshot is malformed")
    expected_keys = (
        _NEXTCLOUD_ROUTING_KEYS
        if backend_id == "nextcloud"
        else _OPENCLOUD_ROUTING_KEYS
    )
    if set(routing) != expected_keys:
        raise ValueError("main-cloud routing snapshot has an unexpected shape")
    if type(routing.get("version")) is not int or routing.get("version") != 1:
        raise ValueError("main-cloud routing snapshot version is unsupported")
    if routing.get("backend_id") != backend_id:
        raise ValueError("main-cloud routing snapshot backend does not match")

    if backend_id == "nextcloud":
        effect_config = routing.get("protected_effect_config_sha256")
        effect_url = routing.get("protected_effect_url")
        if effect_config is not None and (
            not isinstance(effect_config, str)
            or not _SHA256_RE.fullmatch(effect_config)
        ):
            raise ValueError(
                "main-cloud Nextcloud protected-effect configuration is malformed"
            )
        if (effect_url is None) != (effect_config is None):
            raise ValueError(
                "main-cloud Nextcloud protected-effect routing is incomplete"
            )
        return {
            "version": 1,
            "backend_id": backend_id,
            "base_url": _canonical_url(
                routing.get("base_url"), coordinate="Nextcloud base URL"
            ),
            "public_url": _canonical_url(
                routing.get("public_url"), coordinate="Nextcloud public URL"
            ),
            "admin_user": _required_text(
                routing.get("admin_user"), coordinate="Nextcloud admin user"
            ),
            "agent_user": _required_text(
                routing.get("agent_user"), coordinate="Nextcloud agent user"
            ),
            "protected_effect_url": (
                _canonical_url(
                    effect_url,
                    coordinate="Nextcloud protected-effect URL",
                )
                if effect_url is not None
                else None
            ),
            "protected_effect_config_sha256": effect_config,
        }

    quota = routing.get("default_quota_bytes")
    if quota is not None and (type(quota) is not int or quota <= 0):
        raise ValueError("main-cloud OpenCloud quota is malformed")
    insecure = routing.get("mount_insecure_tls")
    if type(insecure) is not bool:
        raise ValueError("main-cloud OpenCloud TLS mode is malformed")
    return {
        "version": 1,
        "backend_id": backend_id,
        "base_url": _canonical_url(
            routing.get("base_url"), coordinate="OpenCloud base URL"
        ),
        "public_url": _canonical_url(
            routing.get("public_url"), coordinate="OpenCloud public URL"
        ),
        "keycloak_issuer": _canonical_url(
            routing.get("keycloak_issuer"),
            coordinate="OpenCloud Keycloak issuer",
        ),
        "keycloak_client_id": _required_text(
            routing.get("keycloak_client_id"),
            coordinate="OpenCloud Keycloak client id",
        ),
        "admin_role_claim_value": _required_text(
            routing.get("admin_role_claim_value"),
            coordinate="OpenCloud admin role",
        ),
        "default_quota_bytes": quota,
        "mount_insecure_tls": insecure,
    }


def _canonical_secret_refs(
    backend_id: str,
    secret_refs: Mapping[str, Any],
) -> dict[str, str]:
    if not isinstance(secret_refs, Mapping):
        raise ValueError("main-cloud secret references are malformed")
    keys = set(secret_refs)
    required = _SECRET_FIELDS[backend_id]
    allowed = required | _OPTIONAL_SECRET_FIELDS[backend_id]
    if not required.issubset(keys) or not keys.issubset(allowed):
        raise ValueError("main-cloud secret references have an unexpected shape")
    result: dict[str, str] = {}
    for field in sorted(keys):
        reference = secret_refs.get(field)
        if not isinstance(reference, str) or not _ENV_SECRET_REF_RE.fullmatch(
            reference
        ):
            raise ValueError("main-cloud secret reference is not an env pointer")
        result[field] = reference
    return result


def main_cloud_installation_proof_sha256(
    *,
    backend_id: str,
    remote_identity: str,
) -> str:
    """Digest a provider-owned stable installation identity without storing it."""

    backend = _canonical_backend_id(backend_id)
    identity = _required_text(
        remote_identity,
        coordinate="installation proof identity",
    )
    payload = _canonical_json(
        {"version": 1, "backend_id": backend, "remote_identity": identity}
    )
    return hashlib.sha256(
        MAIN_CLOUD_INSTALLATION_PROOF_DOMAIN + payload.encode("utf-8")
    ).hexdigest()


class MainCloudBackendInstanceAuthority:
    """Sealed, immutable non-secret snapshot for one backend installation."""

    __slots__ = (
        "_backend_instance_id",
        "_backend_id",
        "_routing",
        "_routing_sha256",
        "_installation_proof_sha256",
        "_secret_refs",
        "_secret_revision",
        "_validation_marker",
        "_sealed",
    )

    def __init__(
        self,
        *,
        backend_instance_id: str,
        backend_id: str,
        routing: dict[str, Any],
        routing_sha256: str,
        installation_proof_sha256: str,
        secret_refs: dict[str, str],
        secret_revision: int,
        _validation_marker: object,
    ) -> None:
        if _validation_marker is not _INSTANCE_MARKER:
            raise ValueError("main-cloud backend instance was not validated")
        object.__setattr__(self, "_backend_instance_id", backend_instance_id)
        object.__setattr__(self, "_backend_id", backend_id)
        object.__setattr__(self, "_routing", routing)
        object.__setattr__(self, "_routing_sha256", routing_sha256)
        object.__setattr__(
            self,
            "_installation_proof_sha256",
            installation_proof_sha256,
        )
        object.__setattr__(self, "_secret_refs", secret_refs)
        object.__setattr__(self, "_secret_revision", secret_revision)
        object.__setattr__(self, "_validation_marker", _validation_marker)
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, name: str, value: Any) -> None:
        if getattr(self, "_sealed", False):
            raise AttributeError("main-cloud backend instance is immutable")
        object.__setattr__(self, name, value)

    @classmethod
    def capture(
        cls,
        *,
        backend_instance_id: str,
        backend_id: str,
        routing: Mapping[str, Any],
        installation_proof_sha256: str,
        secret_refs: Mapping[str, Any],
        secret_revision: int = 1,
    ) -> MainCloudBackendInstanceAuthority:
        backend = _canonical_backend_id(backend_id)
        instance = _canonical_uuid(
            backend_instance_id,
            coordinate="backend instance",
        )
        canonical_routing = _canonical_routing(backend, routing)
        routing_json = _canonical_json(canonical_routing)
        routing_digest = hashlib.sha256(routing_json.encode("utf-8")).hexdigest()
        if not isinstance(installation_proof_sha256, str) or not _SHA256_RE.fullmatch(
            installation_proof_sha256
        ):
            raise ValueError("main-cloud installation proof digest is malformed")
        canonical_refs = _canonical_secret_refs(backend, secret_refs)
        if type(secret_revision) is not int or secret_revision <= 0:
            raise ValueError("main-cloud secret revision is malformed")
        # Round-trip through canonical JSON so neither a caller-owned dict nor
        # nested mutable aliases can change the sealed authority afterward.
        frozen_routing = json.loads(routing_json)
        frozen_refs = json.loads(_canonical_json(canonical_refs))
        return cls(
            backend_instance_id=instance,
            backend_id=backend,
            routing=frozen_routing,
            routing_sha256=routing_digest,
            installation_proof_sha256=installation_proof_sha256,
            secret_refs=frozen_refs,
            secret_revision=secret_revision,
            _validation_marker=_INSTANCE_MARKER,
        )

    @classmethod
    def from_binding(
        cls,
        binding: Mapping[str, Any] | None,
    ) -> MainCloudBackendInstanceAuthority | None:
        if not isinstance(binding, Mapping) or set(binding) != _INSTANCE_BINDING_KEYS:
            return None
        if (
            type(binding.get("version")) is not int
            or binding.get("version") != MAIN_CLOUD_BACKEND_INSTANCE_VERSION
        ):
            return None
        try:
            parsed = cls.capture(
                backend_instance_id=binding.get("backend_instance_id"),
                backend_id=binding.get("backend_id"),
                routing=binding.get("routing"),
                installation_proof_sha256=binding.get("installation_proof_sha256"),
                secret_refs=binding.get("secret_refs"),
                secret_revision=binding.get("secret_revision"),
            )
        except (AttributeError, TypeError, ValueError):
            return None
        if binding.get("routing_sha256") != parsed.routing_sha256:
            return None
        return parsed if parsed.binding == dict(binding) else None

    @property
    def backend_instance_id(self) -> str:
        return self._backend_instance_id

    @property
    def backend_id(self) -> str:
        return self._backend_id

    @property
    def routing(self) -> dict[str, Any]:
        return json.loads(_canonical_json(self._routing))

    @property
    def routing_sha256(self) -> str:
        return self._routing_sha256

    @property
    def installation_proof_sha256(self) -> str:
        return self._installation_proof_sha256

    @property
    def secret_refs(self) -> dict[str, str]:
        return json.loads(_canonical_json(self._secret_refs))

    @property
    def secret_revision(self) -> int:
        return self._secret_revision

    @property
    def binding(self) -> dict[str, Any]:
        return {
            "version": MAIN_CLOUD_BACKEND_INSTANCE_VERSION,
            "backend_instance_id": self.backend_instance_id,
            "backend_id": self.backend_id,
            "routing": self.routing,
            "routing_sha256": self.routing_sha256,
            "installation_proof_sha256": self.installation_proof_sha256,
            "secret_refs": self.secret_refs,
            "secret_revision": self.secret_revision,
        }

    @property
    def canonical_json(self) -> str:
        return _canonical_json(self.binding)

    @property
    def authority_sha256(self) -> str:
        return hashlib.sha256(self.canonical_json.encode("utf-8")).hexdigest()


__all__ = [
    "MAIN_CLOUD_BACKEND_INSTANCE_VERSION",
    "MAIN_CLOUD_INSTALLATION_PROOF_DOMAIN",
    "MainCloudBackendInstanceAuthority",
    "main_cloud_installation_proof_sha256",
]
