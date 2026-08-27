"""Fail-closed Collabora discovery and WOPI token authority for Canvas Office.

The workspace file remains the content authority.  Tokens identify one user,
thread, canonical path, and access direction; every WOPI call re-checks that
live relationship before any bytes are materialized.
"""

from __future__ import annotations

import asyncio
import os
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit
from uuid import UUID, uuid4
from xml.etree import ElementTree

import httpx
import jwt

from services.canvas import CanvasRecord, WorkspaceFileSource
from services.canvas_files import ThreadWorkspaceFileGateway, canonical_workspace_path

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_OFFICE_EXTENSIONS = frozenset({"docx", "xlsx", "pptx", "odt", "ods", "odp"})
_FILE_ID_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_ALGORITHM = "HS256"
_AUDIENCE = "wopi"


class CanvasOfficeError(Exception):
    """Typed deployment, discovery, or WOPI authorization failure."""

    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def _truthy(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in _TRUTHY


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise CanvasOfficeError(
            503,
            "canvas_office_configuration_invalid",
            f"{name} must be an integer",
        ) from exc
    if not minimum <= value <= maximum:
        raise CanvasOfficeError(
            503,
            "canvas_office_configuration_invalid",
            f"{name} must be between {minimum} and {maximum}",
        )
    return value


def _bounded_float(name: str, default: float, minimum: float, maximum: float) -> float:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise CanvasOfficeError(
            503,
            "canvas_office_configuration_invalid",
            f"{name} must be a number",
        ) from exc
    if not minimum <= value <= maximum:
        raise CanvasOfficeError(
            503,
            "canvas_office_configuration_invalid",
            f"{name} must be between {minimum} and {maximum}",
        )
    return value


def _normalized_http_url(value: str, *, name: str, origin_only: bool) -> str:
    parsed = urlsplit(value.strip())
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or not parsed.hostname.isascii()
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (origin_only and parsed.path not in {"", "/"})
    ):
        raise CanvasOfficeError(
            503,
            "canvas_office_configuration_invalid",
            f"{name} must be an absolute HTTP(S) {'origin' if origin_only else 'URL'}",
        )
    try:
        port = parsed.port
    except ValueError as exc:
        raise CanvasOfficeError(
            503,
            "canvas_office_configuration_invalid",
            f"{name} contains an invalid port",
        ) from exc
    default_port = 443 if parsed.scheme == "https" else 80
    authority = parsed.hostname.lower()
    if port is not None and port != default_port:
        authority = f"{authority}:{port}"
    path = "" if origin_only else parsed.path.rstrip("/")
    return f"{parsed.scheme}://{authority}{path}"


@dataclass(frozen=True, slots=True)
class CollaboraConfig:
    enabled: bool
    internal_url: str
    public_origin: str
    wopi_base_url: str
    cockpit_origin: str
    token_ttl_seconds: int
    discovery_cache_ttl_seconds: int
    request_timeout_seconds: float

    def require_enabled(self) -> "CollaboraConfig":
        if not self.enabled:
            raise CanvasOfficeError(
                503,
                "canvas_office_unavailable",
                "Office document viewing is not enabled for this deployment",
            )
        for value, name, origin_only in (
            (self.internal_url, "COLLABORA_INTERNAL_URL", False),
            (self.public_origin, "COLLABORA_PUBLIC_URL", True),
            (self.wopi_base_url, "COLLABORA_WOPI_BASE_URL", False),
            (self.cockpit_origin, "COLLABORA_COCKPIT_ORIGIN", True),
        ):
            _normalized_http_url(value, name=name, origin_only=origin_only)
        return self

    def require_cockpit_origin(self, value: str | None) -> str:
        self.require_enabled()
        if value is None:
            raise CanvasOfficeError(
                403,
                "canvas_office_origin_denied",
                "A trusted Cockpit Origin header is required",
            )
        normalized = _normalized_http_url(
            value,
            name="Origin",
            origin_only=True,
        )
        if not secrets.compare_digest(normalized, self.cockpit_origin):
            raise CanvasOfficeError(
                403,
                "canvas_office_origin_denied",
                "The embedding Cockpit origin is not allowed",
            )
        return normalized


def collabora_config() -> CollaboraConfig:
    """Load the opt-in deployment gate without making disabled installs brittle."""

    enabled = _truthy("COLLABORA_ENABLED")
    config = CollaboraConfig(
        enabled=enabled,
        internal_url=os.getenv("COLLABORA_INTERNAL_URL", "").strip().rstrip("/"),
        public_origin=os.getenv("COLLABORA_PUBLIC_URL", "").strip().rstrip("/"),
        wopi_base_url=os.getenv("COLLABORA_WOPI_BASE_URL", "").strip().rstrip("/"),
        cockpit_origin=os.getenv("COLLABORA_COCKPIT_ORIGIN", "").strip().rstrip("/"),
        token_ttl_seconds=_bounded_int(
            "COLLABORA_TOKEN_TTL_SECONDS",
            36_000,
            3_600,
            86_400,
        ),
        discovery_cache_ttl_seconds=_bounded_int(
            "COLLABORA_DISCOVERY_CACHE_TTL_SECONDS",
            21_600,
            60,
            86_400,
        ),
        request_timeout_seconds=_bounded_float(
            "COLLABORA_DISCOVERY_TIMEOUT_SECONDS",
            5.0,
            0.25,
            30.0,
        ),
    )
    if enabled:
        config.require_enabled()
    return config


def _normalize_extension(extension: str) -> str:
    normalized = extension.strip().lower().removeprefix(".")
    if normalized not in _OFFICE_EXTENSIONS:
        raise CanvasOfficeError(
            422,
            "unsupported_canvas_file",
            "This Office document extension is not supported",
        )
    return normalized


def _parse_discovery(xml_text: str, public_origin: str) -> dict[str, str]:
    if "<!doctype" in xml_text.lower() or "<!entity" in xml_text.lower():
        raise CanvasOfficeError(
            503,
            "canvas_office_unavailable",
            "Collabora discovery returned unsafe XML",
        )
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as exc:
        raise CanvasOfficeError(
            503,
            "canvas_office_unavailable",
            "Collabora discovery returned invalid XML",
        ) from exc

    actions: dict[str, tuple[int, str]] = {}
    for action in root.iter("action"):
        extension = str(action.attrib.get("ext") or "").strip().lower()
        name = str(action.attrib.get("name") or "").strip().lower()
        urlsrc = str(action.attrib.get("urlsrc") or "").strip()
        if extension not in _OFFICE_EXTENSIONS or name not in {"view", "edit"}:
            continue
        try:
            parsed = urlsplit(urlsrc)
            origin = _normalized_http_url(
                f"{parsed.scheme}://{parsed.netloc}",
                name="Collabora discovery urlsrc",
                origin_only=True,
            )
        except (CanvasOfficeError, ValueError):
            continue
        if (
            origin != public_origin
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or not parsed.path.endswith("/cool.html")
        ):
            continue
        # A dedicated view action wins. Collabora uses the same endpoint for
        # both modes; CheckFileInfo remains the authority for write capability.
        priority = 0 if name == "view" else 1
        current = actions.get(extension)
        if current is None or priority < current[0]:
            actions[extension] = (priority, urlsrc)

    resolved = {extension: value[1] for extension, value in actions.items()}
    if not resolved:
        raise CanvasOfficeError(
            503,
            "canvas_office_unavailable",
            "Collabora discovery has no supported Office actions",
        )
    return resolved


class CollaboraDiscoveryService:
    """Hours-long discovery cache with stale-on-error availability."""

    def __init__(
        self,
        config: CollaboraConfig,
        *,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._config = config
        self._client = client
        self._clock = clock
        self._actions: dict[str, str] = {}
        self._refreshed_at: float | None = None
        self._lock = asyncio.Lock()

    @property
    def available(self) -> bool:
        return self._config.enabled and bool(self._actions)

    async def warm(self) -> bool:
        """Best-effort startup fetch; disabled/cold failures remain dark."""

        if not self._config.enabled:
            return False
        try:
            await self.refresh(force=True)
        except CanvasOfficeError:
            return False
        return self.available

    async def get_urlsrc(self, extension: str) -> str:
        self._config.require_enabled()
        normalized = _normalize_extension(extension)
        await self.refresh(force=False)
        urlsrc = self._actions.get(normalized)
        if urlsrc is None:
            raise CanvasOfficeError(
                503,
                "canvas_office_unavailable",
                "Collabora does not advertise this Office document format",
            )
        return urlsrc

    async def refresh(self, *, force: bool = True) -> bool:
        """Refresh discovery/capabilities, retaining a prior good cache on error."""

        self._config.require_enabled()
        async with self._lock:
            now = self._clock()
            if (
                not force
                and self._refreshed_at is not None
                and now - self._refreshed_at < self._config.discovery_cache_ttl_seconds
            ):
                return False
            try:
                discovery, capabilities = await self._fetch()
                actions = _parse_discovery(
                    discovery.text,
                    self._config.public_origin,
                )
                capability_payload = capabilities.json()
                if not isinstance(capability_payload, dict):
                    raise ValueError("capabilities payload must be an object")
            except Exception as exc:
                if self._actions:
                    return False
                if isinstance(exc, CanvasOfficeError):
                    raise
                raise CanvasOfficeError(
                    503,
                    "canvas_office_unavailable",
                    "Collabora discovery is unavailable",
                ) from exc
            self._actions = actions
            self._refreshed_at = now
            return True

    async def _fetch(self) -> tuple[httpx.Response, httpx.Response]:
        async def request_pair(client: httpx.AsyncClient):
            discovery_request = client.get(
                f"{self._config.internal_url}/hosting/discovery"
            )
            capabilities_request = client.get(
                f"{self._config.internal_url}/hosting/capabilities"
            )
            discovery, capabilities = await asyncio.gather(
                discovery_request,
                capabilities_request,
            )
            discovery.raise_for_status()
            capabilities.raise_for_status()
            return discovery, capabilities

        if self._client is not None:
            return await request_pair(self._client)
        timeout = httpx.Timeout(self._config.request_timeout_seconds)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            return await request_pair(client)


@dataclass(frozen=True, slots=True)
class WopiTokenGrant:
    access_token: str
    file_id: str
    expires_at_ms: int


@dataclass(frozen=True, slots=True)
class WopiAccess:
    user: dict[str, Any]
    thread: dict[str, Any]
    record: CanvasRecord
    claims: dict[str, Any]


UserLoader = Callable[[str], Awaitable[dict[str, Any] | None]]
ThreadLoader = Callable[[str], Awaitable[dict[str, Any] | None]]
CanvasLoader = Callable[[str], Awaitable[CanvasRecord | None]]
EditingChecker = Callable[[dict[str, Any], CanvasRecord], bool]


def wopi_file_id(thread_id: str, path: str) -> str:
    """Return the stable path-presentation identity used by Collabora."""

    import hashlib

    canonical = canonical_workspace_path(path)
    material = (
        b"srw-canvas-wopi-file-v1\x00"
        + str(thread_id).encode("utf-8")
        + b"\x00"
        + canonical.encode("utf-8")
    )
    return hashlib.sha256(material).hexdigest()


class WopiTokenService:
    """Mint HS256 WOPI JWTs and re-admit every use against live Canvas state."""

    def __init__(
        self,
        secret: str,
        *,
        ttl_seconds: int,
        user_loader: UserLoader,
        thread_loader: ThreadLoader,
        canvas_loader: CanvasLoader,
        editing_checker: EditingChecker | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not secret:
            raise CanvasOfficeError(
                503,
                "canvas_office_configuration_invalid",
                "SESSION_JWT_SECRET is required for Office document viewing",
            )
        if not 60 <= int(ttl_seconds) <= 86_400:
            raise ValueError("WOPI token TTL must be between 60 and 86400 seconds")
        self._secret = secret
        self._ttl = int(ttl_seconds)
        self._user_loader = user_loader
        self._thread_loader = thread_loader
        self._canvas_loader = canvas_loader
        self._editing_checker = editing_checker
        self._clock = clock

    def mint(
        self,
        *,
        user_id: str,
        thread_id: str,
        path: str,
        write_flag: bool,
    ) -> WopiTokenGrant:
        canonical = canonical_workspace_path(path)
        now = int(self._clock())
        exp = now + self._ttl
        claims = {
            "sub": str(user_id),
            "tid": str(thread_id),
            "path": canonical,
            "write_flag": bool(write_flag),
            "aud": _AUDIENCE,
            "iat": now,
            "exp": exp,
            "jti": str(uuid4()),
        }
        token = jwt.encode(claims, self._secret, algorithm=_ALGORITHM)
        return WopiTokenGrant(
            access_token=token,
            file_id=wopi_file_id(thread_id, canonical),
            expires_at_ms=exp * 1000,
        )

    def validate(self, token: str) -> dict[str, Any]:
        try:
            claims = jwt.decode(
                token,
                self._secret,
                algorithms=[_ALGORITHM],
                audience=_AUDIENCE,
                options={
                    "require": [
                        "aud",
                        "exp",
                        "iat",
                        "jti",
                        "path",
                        "sub",
                        "tid",
                        "write_flag",
                    ],
                    "verify_exp": False,
                    "verify_iat": False,
                },
            )
            now = int(self._clock())
            if (
                not isinstance(claims.get("sub"), str)
                or not claims["sub"]
                or not isinstance(claims.get("tid"), str)
                or not claims["tid"]
                or not isinstance(claims.get("path"), str)
                or canonical_workspace_path(claims["path"]) != claims["path"]
                or type(claims.get("write_flag")) is not bool
                or type(claims.get("iat")) is not int
                or type(claims.get("exp")) is not int
                or claims["iat"] > now
                or claims["exp"] <= now
                or claims["exp"] - claims["iat"] != self._ttl
                or not isinstance(claims.get("jti"), str)
                or str(UUID(claims["jti"])) != claims["jti"]
            ):
                raise ValueError("invalid WOPI claim shape")
            return dict(claims)
        except (jwt.PyJWTError, TypeError, ValueError) as exc:
            raise CanvasOfficeError(
                401,
                "wopi_token_invalid",
                "WOPI access token is invalid or expired",
            ) from exc

    async def authenticate(
        self,
        token: str,
        *,
        file_id: str,
        require_write: bool,
    ) -> WopiAccess:
        if not _FILE_ID_PATTERN.fullmatch(file_id):
            raise CanvasOfficeError(
                401, "wopi_token_invalid", "WOPI file identity is invalid"
            )
        claims = self.validate(token)
        if type(require_write) is not bool or (
            require_write and claims["write_flag"] is not True
        ):
            raise CanvasOfficeError(
                403, "wopi_access_denied", "WOPI token scope does not permit this call"
            )
        expected_file_id = wopi_file_id(claims["tid"], claims["path"])
        if not secrets.compare_digest(file_id, expected_file_id):
            raise CanvasOfficeError(
                403, "wopi_access_denied", "WOPI token does not match this file"
            )

        user, thread, record = await asyncio.gather(
            self._user_loader(claims["sub"]),
            self._thread_loader(claims["tid"]),
            self._canvas_loader(claims["tid"]),
        )
        if (
            user is None
            or str(user.get("id") or "") != claims["sub"]
            or not bool(user.get("is_approved"))
            or thread is None
            or str(thread.get("id") or "") != claims["tid"]
            or (
                not bool(user.get("is_admin"))
                and str(thread.get("user_id") or "") != claims["sub"]
            )
            or record is None
            or record.renderer != "office"
            or record.editable is not claims["write_flag"]
            or not isinstance(record.source, WorkspaceFileSource)
            or record.source.path != claims["path"]
            or record.thread_id != claims["tid"]
            or (
                record.editable
                and (
                    self._editing_checker is None
                    or not self._editing_checker(thread, record)
                )
            )
        ):
            raise CanvasOfficeError(
                403,
                "wopi_access_denied",
                "WOPI access is no longer authorized by the current Canvas",
            )
        return WopiAccess(
            user=dict(user),
            thread=dict(thread),
            record=record,
            claims=claims,
        )


_discovery_singleton: CollaboraDiscoveryService | None = None
_discovery_config: CollaboraConfig | None = None


def get_collabora_discovery_service() -> CollaboraDiscoveryService:
    global _discovery_singleton, _discovery_config
    config = collabora_config()
    if _discovery_singleton is None or config != _discovery_config:
        _discovery_singleton = CollaboraDiscoveryService(config)
        _discovery_config = config
    return _discovery_singleton


async def warm_collabora_discovery() -> bool:
    try:
        return await get_collabora_discovery_service().warm()
    except CanvasOfficeError:
        return False


def create_wopi_token_service(db: Any) -> WopiTokenService:
    from services.canvas import CanvasService

    config = collabora_config().require_enabled()
    gateway = ThreadWorkspaceFileGateway(
        thread_loader=getattr(db, "get_thread", None),
    )
    return WopiTokenService(
        os.getenv("SESSION_JWT_SECRET", ""),
        ttl_seconds=config.token_ttl_seconds,
        user_loader=db.get_user,
        thread_loader=db.get_thread,
        canvas_loader=CanvasService(db).get,
        editing_checker=gateway.supports_editing,
    )


__all__ = [
    "CanvasOfficeError",
    "CollaboraConfig",
    "CollaboraDiscoveryService",
    "WopiAccess",
    "WopiTokenGrant",
    "WopiTokenService",
    "collabora_config",
    "create_wopi_token_service",
    "get_collabora_discovery_service",
    "warm_collabora_discovery",
    "wopi_file_id",
]
