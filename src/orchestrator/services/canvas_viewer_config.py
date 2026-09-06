"""Fail-closed deployment configuration for the isolated Canvas viewer.

The callable ``workspace_port`` source and the browser-facing viewer are two
different gates.  A deployment may validate/persist an app pointer without
publishing a wildcard user-content origin.  Viewer configuration is therefore
considered usable only when every isolation attestation below is explicit.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
import re
from urllib.parse import urlsplit
from uuid import UUID

from orchestrator.services.canvas_apps import canvas_live_preview_enabled

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_HOST_SUFFIX = re.compile(
    r"^\.(?=.{4,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)
_DNS_HOST = re.compile(
    r"^(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)
_COOKIE_MODES = frozenset({"development-cookie-free", "psl-isolated"})
_PROFILES = frozenset({"development", "production"})


class CanvasViewerConfigurationError(ValueError):
    """The viewer was enabled without satisfying its isolation contract."""


def _truthy(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in _TRUTHY


def _bounded_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise CanvasViewerConfigurationError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise CanvasViewerConfigurationError(
            f"{name} must be between {minimum} and {maximum}"
        )
    return value


def _origin(value: str) -> str:
    parsed = urlsplit(value.strip())
    hostname = (parsed.hostname or "").lower()
    if (
        parsed.scheme not in {"http", "https"}
        or not hostname.isascii()
        or not _DNS_HOST.fullmatch(hostname)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise CanvasViewerConfigurationError(
            "CANVAS_VIEWER_COCKPIT_ORIGINS contains an invalid origin"
        )
    try:
        port = parsed.port
    except ValueError as exc:
        raise CanvasViewerConfigurationError(
            "CANVAS_VIEWER_COCKPIT_ORIGINS contains an invalid port"
        ) from exc
    default = 443 if parsed.scheme == "https" else 80
    authority = hostname
    if port is not None and port != default:
        authority = f"{authority}:{port}"
    return f"{parsed.scheme}://{authority}"


@dataclass(frozen=True, slots=True)
class CanvasViewerConfig:
    enabled: bool
    host_suffix: str
    cookie_mode: str
    deployment_profile: str
    cockpit_origins: frozenset[str]
    session_ttl_seconds: int
    bootstrap_ttl_seconds: int
    attachment_ttl_seconds: int
    revalidate_seconds: int

    def host_for_origin(self, generation: UUID) -> str:
        if not self.enabled:
            raise CanvasViewerConfigurationError("Canvas viewer is disabled")
        return f"{str(generation)}{self.host_suffix}"

    def public_origin(self, generation: UUID) -> str:
        return f"https://{self.host_for_origin(generation)}"

    def generation_for_host(self, host: str) -> UUID:
        """Resolve only one exact ``UUID + configured suffix`` hostname."""

        candidate = host.strip().lower()
        if not self.enabled or not candidate or ":" in candidate:
            raise CanvasViewerConfigurationError("Canvas viewer host is invalid")
        if not candidate.endswith(self.host_suffix):
            raise CanvasViewerConfigurationError("Canvas viewer host is invalid")
        label = candidate[: -len(self.host_suffix)]
        if not label or "." in label:
            raise CanvasViewerConfigurationError("Canvas viewer host is invalid")
        try:
            generation = UUID(label)
        except (TypeError, ValueError) as exc:
            raise CanvasViewerConfigurationError(
                "Canvas viewer host is invalid"
            ) from exc
        if str(generation) != label:
            raise CanvasViewerConfigurationError("Canvas viewer host is invalid")
        return generation

    def require_cockpit_origin(self, value: str | None) -> str:
        if not self.enabled or value is None:
            raise CanvasViewerConfigurationError(
                "A trusted Cockpit Origin header is required"
            )
        normalized = _origin(value)
        if normalized not in self.cockpit_origins:
            raise CanvasViewerConfigurationError(
                "The embedding Cockpit origin is not allowed"
            )
        return normalized


def canvas_viewer_config() -> CanvasViewerConfig:
    """Load and validate the viewer gate from the current environment."""

    requested = _truthy("CANVAS_VIEWER_ENABLED")
    enabled = requested and canvas_live_preview_enabled()
    profile = os.getenv("CANVAS_VIEWER_DEPLOYMENT_PROFILE", "production").strip()
    cookie_mode = os.getenv("CANVAS_VIEWER_COOKIE_MODE", "").strip()
    domain = os.getenv("CANVAS_VIEWER_DOMAIN", "").strip().lower()
    suffix = os.getenv("CANVAS_VIEWER_HOST_SUFFIX", "").strip().lower()
    raw_origins = os.getenv("CANVAS_VIEWER_COCKPIT_ORIGINS", "").split(",")

    session_ttl = _bounded_int(
        "CANVAS_VIEWER_SESSION_TTL_SECONDS", 900, minimum=60, maximum=3600
    )
    bootstrap_ttl = _bounded_int(
        "CANVAS_VIEWER_BOOTSTRAP_TTL_SECONDS", 60, minimum=15, maximum=300
    )
    attachment_ttl = _bounded_int(
        "CANVAS_VIEWER_ATTACHMENT_TTL_SECONDS", 1200, minimum=60, maximum=7200
    )
    revalidate = _bounded_int(
        "CANVAS_VIEWER_REVALIDATE_SECONDS", 15, minimum=1, maximum=15
    )

    # A disabled viewer deliberately tolerates absent domain settings so the
    # same image can run in existing installations.  It still returns bounded
    # timing values for cleanup/tests, but no caller may mint an origin.
    if not enabled:
        return CanvasViewerConfig(
            enabled=False,
            host_suffix="",
            cookie_mode="disabled",
            deployment_profile=profile if profile in _PROFILES else "production",
            cockpit_origins=frozenset(),
            session_ttl_seconds=session_ttl,
            bootstrap_ttl_seconds=bootstrap_ttl,
            attachment_ttl_seconds=attachment_ttl,
            revalidate_seconds=revalidate,
        )

    if profile not in _PROFILES:
        raise CanvasViewerConfigurationError(
            "CANVAS_VIEWER_DEPLOYMENT_PROFILE must be development or production"
        )
    if cookie_mode not in _COOKIE_MODES:
        raise CanvasViewerConfigurationError(
            "CANVAS_VIEWER_COOKIE_MODE must be development-cookie-free or psl-isolated"
        )
    if not _HOST_SUFFIX.fullmatch(suffix):
        raise CanvasViewerConfigurationError(
            "CANVAS_VIEWER_HOST_SUFFIX must be a dotted ASCII DNS suffix"
        )
    if (
        not domain.isascii()
        or domain.count(".") < 1
        or not _DNS_HOST.fullmatch(domain)
        or not suffix.endswith(f".{domain}")
    ):
        raise CanvasViewerConfigurationError(
            "CANVAS_VIEWER_DOMAIN must be an ASCII parent of CANVAS_VIEWER_HOST_SUFFIX"
        )
    origins = frozenset(_origin(item) for item in raw_origins if item.strip())
    if not origins:
        raise CanvasViewerConfigurationError(
            "CANVAS_VIEWER_COCKPIT_ORIGINS must contain at least one trusted origin"
        )
    if any(not origin.startswith("https://") for origin in origins):
        raise CanvasViewerConfigurationError(
            "Enabled Canvas viewers require HTTPS Cockpit origins"
        )
    # Raw-path preservation is deliberately NOT an attestation gate: a
    # normalizing proxy (Cloudflare tunnel, ordinary ingress) in front of the
    # gateway canonicalizes before the security boundary, which is the safe
    # direction — the gateway checks and forwards the same string. See
    # knowledge-base/knowledge/issues/canvas_hosted_edge_use_cloudflare_tunnel.md.
    if profile == "production":
        if cookie_mode != "psl-isolated":
            raise CanvasViewerConfigurationError(
                "Production Canvas viewers require psl-isolated cookie mode"
            )
        if suffix != f".{domain}":
            raise CanvasViewerConfigurationError(
                "Production Canvas viewers require CANVAS_VIEWER_HOST_SUFFIX "
                "to equal the exact effective-PSL CANVAS_VIEWER_DOMAIN"
            )
        if not _truthy("CANVAS_VIEWER_PSL_BOUNDARY_VERIFIED"):
            raise CanvasViewerConfigurationError(
                "Production Canvas viewers require an attested PSL boundary"
            )
    elif cookie_mode == "development-cookie-free":
        # The mode name is intentionally explicit in deployment manifests and
        # response diagnostics. Application Cookie/Set-Cookie fields are then
        # removed by the proxy; this is not a production isolation claim.
        pass

    return CanvasViewerConfig(
        enabled=True,
        host_suffix=suffix,
        cookie_mode=cookie_mode,
        deployment_profile=profile,
        cockpit_origins=origins,
        session_ttl_seconds=session_ttl,
        bootstrap_ttl_seconds=bootstrap_ttl,
        attachment_ttl_seconds=attachment_ttl,
        revalidate_seconds=revalidate,
    )


__all__ = [
    "CanvasViewerConfig",
    "CanvasViewerConfigurationError",
    "canvas_viewer_config",
]
