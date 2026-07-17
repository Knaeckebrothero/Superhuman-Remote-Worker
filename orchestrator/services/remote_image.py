"""Explicit-consent, SSRF-hardened remote raster image fetching.

The Cockpit calls this service only after a user reviews an exact URL and
chooses "Load image once". The browser never receives that remote URL as an
element source. This module is deliberately independent of FastAPI so its
network and byte-validation boundaries can be tested directly.
"""

from __future__ import annotations

import asyncio
import io
import ipaddress
import socket
import warnings
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Sequence
from urllib.parse import urljoin, urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver
from PIL import Image, UnidentifiedImageError

MAX_REMOTE_IMAGE_URL_CHARS = 8_192
MAX_REMOTE_IMAGE_BYTES = 10 * 1024 * 1024
MAX_REMOTE_IMAGE_PIXELS = 40_000_000
MAX_REMOTE_IMAGE_FRAMES = 200
MAX_REMOTE_IMAGE_REDIRECTS = 3
REMOTE_IMAGE_TOTAL_TIMEOUT_SECONDS = 15.0
REMOTE_IMAGE_CONNECT_TIMEOUT_SECONDS = 5.0
REMOTE_IMAGE_READ_TIMEOUT_SECONDS = 8.0

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_IMAGE_MEDIA_TYPES = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "GIF": "image/gif",
    "WEBP": "image/webp",
}
_FETCH_SEMAPHORE = asyncio.Semaphore(8)


class RemoteImageError(Exception):
    """A bounded, URL-free error safe to map to an API response."""

    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class RemoteImage:
    content: bytes
    media_type: str
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class RemoteImageTarget:
    url: str
    host: str
    port: int


@dataclass(frozen=True, slots=True)
class ResolvedAddress:
    address: str
    family: int
    protocol: int = 0


AddressLookup = Callable[[str, int], Awaitable[Sequence[ResolvedAddress]]]


def is_public_address(value: str) -> bool:
    """Return whether an IP is globally routable and safe for this fetcher."""

    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return is_public_address(str(address.ipv4_mapped))
        # Transition addresses obscure the ultimate IPv4 destination.
        if address.sixtofour is not None or address.teredo is not None:
            return False
    return bool(
        address.is_global
        and not address.is_private
        and not address.is_loopback
        and not address.is_link_local
        and not address.is_multicast
        and not address.is_reserved
        and not address.is_unspecified
    )


def validate_remote_image_url(url: str) -> RemoteImageTarget:
    """Validate an exact user-reviewed URL before any DNS or network activity."""

    if (
        not url
        or len(url) > MAX_REMOTE_IMAGE_URL_CHARS
        or url != url.strip()
        or any(ord(char) <= 0x20 or ord(char) == 0x7F for char in url)
    ):
        raise RemoteImageError(
            400,
            "remote_image_url_invalid",
            "The image URL is invalid",
        )

    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise RemoteImageError(
            400,
            "remote_image_url_invalid",
            "The image URL is invalid",
        ) from exc

    if parsed.scheme.lower() != "https" or not parsed.netloc or not parsed.hostname:
        raise RemoteImageError(
            400,
            "remote_image_https_required",
            "Only HTTPS image URLs are supported",
        )
    if parsed.username or parsed.password:
        raise RemoteImageError(
            400,
            "remote_image_credentials_forbidden",
            "Image URLs cannot contain credentials",
        )
    if parsed.fragment:
        raise RemoteImageError(
            400,
            "remote_image_fragment_forbidden",
            "Image URLs cannot contain fragments",
        )
    if port not in (None, 443):
        raise RemoteImageError(
            400,
            "remote_image_port_forbidden",
            "Only the standard HTTPS port is supported",
        )

    raw_host = parsed.hostname
    if raw_host.endswith(".") or "%" in raw_host:
        raise RemoteImageError(
            400,
            "remote_image_host_invalid",
            "The image host is invalid",
        )
    try:
        host = raw_host.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise RemoteImageError(
            400,
            "remote_image_host_invalid",
            "The image host is invalid",
        ) from exc
    if not host or len(host) > 253:
        raise RemoteImageError(
            400,
            "remote_image_host_invalid",
            "The image host is invalid",
        )

    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        labels = host.split(".")
        if any(
            not label
            or len(label) > 63
            or label.startswith("-")
            or label.endswith("-")
            or not all(char.isalnum() or char == "-" for char in label)
            for label in labels
        ):
            raise RemoteImageError(
                400,
                "remote_image_host_invalid",
                "The image host is invalid",
            )
        if host == "localhost" or host.endswith(".localhost"):
            raise RemoteImageError(
                403,
                "remote_image_destination_blocked",
                "The image destination is not public",
            )
    else:
        if not is_public_address(str(literal)):
            raise RemoteImageError(
                403,
                "remote_image_destination_blocked",
                "The image destination is not public",
            )

    return RemoteImageTarget(url=url, host=host, port=443)


async def _system_address_lookup(host: str, port: int) -> Sequence[ResolvedAddress]:
    loop = asyncio.get_running_loop()
    try:
        records = await loop.getaddrinfo(
            host,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
        )
    except OSError as exc:
        raise RemoteImageError(
            502,
            "remote_image_dns_failed",
            "The image host could not be resolved",
        ) from exc
    return tuple(
        ResolvedAddress(
            address=str(sockaddr[0]),
            family=int(family),
            protocol=int(protocol),
        )
        for family, _type, protocol, _canonname, sockaddr in records
    )


class PinnedPublicResolver(AbstractResolver):
    """Resolve once, reject mixed/private answers, and pin the connection IPs."""

    def __init__(self, lookup: AddressLookup = _system_address_lookup):
        self._lookup = lookup
        self._pinned: dict[tuple[str, int], list[dict[str, Any]]] = {}

    async def pin(self, host: str, port: int) -> list[dict[str, Any]]:
        canonical = host.encode("idna").decode("ascii").lower()
        key = (canonical, port)
        existing = self._pinned.get(key)
        if existing is not None:
            return existing

        try:
            literal = ipaddress.ip_address(canonical)
        except ValueError:
            addresses = await self._lookup(canonical, port)
        else:
            family = socket.AF_INET6 if literal.version == 6 else socket.AF_INET
            addresses = (ResolvedAddress(str(literal), family),)

        if not addresses:
            raise RemoteImageError(
                502,
                "remote_image_dns_failed",
                "The image host could not be resolved",
            )

        results: list[dict[str, Any]] = []
        seen: set[str] = set()
        for record in addresses:
            try:
                address = ipaddress.ip_address(record.address)
            except ValueError as exc:
                raise RemoteImageError(
                    502,
                    "remote_image_dns_failed",
                    "The image host returned an invalid address",
                ) from exc
            normalized = address.compressed
            # Reject the whole hostname if any answer could reach a protected
            # network; selecting only its public answer would be unsafe under
            # split-horizon DNS and retries.
            if not is_public_address(normalized):
                raise RemoteImageError(
                    403,
                    "remote_image_destination_blocked",
                    "The image destination is not public",
                )
            if normalized in seen:
                continue
            seen.add(normalized)
            results.append(
                {
                    "hostname": canonical,
                    "host": normalized,
                    "port": port,
                    "family": (
                        socket.AF_INET6 if address.version == 6 else socket.AF_INET
                    ),
                    "proto": record.protocol,
                    "flags": socket.AI_NUMERICHOST,
                }
            )

        self._pinned[key] = results
        return results

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: socket.AddressFamily = socket.AF_INET,
    ) -> list[dict[str, Any]]:
        del family
        try:
            return await self.pin(host, port)
        except RemoteImageError as exc:
            # aiohttp's connector expects resolver failures to be OSError-like.
            # The explicit preflight in `fetch_remote_image` preserves the
            # structured policy error for normal calls.
            raise OSError(exc.message) from exc

    async def close(self) -> None:
        self._pinned.clear()


def validate_remote_image_bytes(data: bytes) -> RemoteImage:
    """Accept only bounded, decodable PNG/JPEG/GIF/WebP raster bytes."""

    if not data:
        raise RemoteImageError(
            422,
            "remote_image_invalid",
            "The remote response was not a valid image",
        )
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            with Image.open(io.BytesIO(data)) as image:
                image_format = str(image.format or "").upper()
                frame_count = int(getattr(image, "n_frames", 1))
                if frame_count < 1 or frame_count > MAX_REMOTE_IMAGE_FRAMES:
                    raise RemoteImageError(
                        413,
                        "remote_image_too_large",
                        "The image has too many frames",
                    )
                total_pixels = 0
                width, height = image.size
                for frame in range(frame_count):
                    image.seek(frame)
                    frame_width, frame_height = image.size
                    if frame_width <= 0 or frame_height <= 0:
                        raise ValueError("invalid image dimensions")
                    total_pixels += frame_width * frame_height
                    if total_pixels > MAX_REMOTE_IMAGE_PIXELS:
                        raise RemoteImageError(
                            413,
                            "remote_image_too_large",
                            "The decoded image is too large",
                        )
                image.verify()
    except RemoteImageError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Warning) as exc:
        raise RemoteImageError(
            422,
            "remote_image_invalid",
            "The remote response was not a valid image",
        ) from exc

    media_type = _IMAGE_MEDIA_TYPES.get(image_format)
    if media_type is None:
        raise RemoteImageError(
            415,
            "remote_image_format_unsupported",
            "Only PNG, JPEG, GIF, and WebP images are supported",
        )
    return RemoteImage(
        content=data,
        media_type=media_type,
        width=width,
        height=height,
    )


def _new_client_session(resolver: AbstractResolver) -> aiohttp.ClientSession:
    connector = aiohttp.TCPConnector(
        resolver=resolver,
        family=socket.AF_UNSPEC,
        use_dns_cache=True,
        ttl_dns_cache=REMOTE_IMAGE_TOTAL_TIMEOUT_SECONDS,
        limit=2,
    )
    timeout = aiohttp.ClientTimeout(
        total=REMOTE_IMAGE_TOTAL_TIMEOUT_SECONDS,
        connect=REMOTE_IMAGE_CONNECT_TIMEOUT_SECONDS,
        sock_read=REMOTE_IMAGE_READ_TIMEOUT_SECONDS,
    )
    return aiohttp.ClientSession(
        connector=connector,
        timeout=timeout,
        trust_env=False,
        auto_decompress=False,
        cookie_jar=aiohttp.DummyCookieJar(),
        headers={
            "Accept": "image/png,image/jpeg,image/gif,image/webp;q=0.9,*/*;q=0.1",
            "Accept-Encoding": "identity",
            "User-Agent": "SRW-Remote-Image/1.0",
        },
    )


async def fetch_remote_image(
    url: str,
    *,
    resolver: PinnedPublicResolver | None = None,
    session_factory: Callable[[AbstractResolver], Any] | None = None,
) -> RemoteImage:
    """Fetch one reviewed URL without forwarding browser identity or headers."""

    target = validate_remote_image_url(url)
    active_resolver = resolver or PinnedPublicResolver()
    make_session = session_factory or _new_client_session

    try:
        async with asyncio.timeout(REMOTE_IMAGE_TOTAL_TIMEOUT_SECONDS):
            async with _FETCH_SEMAPHORE:
                await active_resolver.pin(target.host, target.port)
                async with make_session(active_resolver) as session:
                    current = target.url
                    visited: set[str] = set()
                    for redirect_count in range(MAX_REMOTE_IMAGE_REDIRECTS + 1):
                        current_target = validate_remote_image_url(current)
                        await active_resolver.pin(
                            current_target.host,
                            current_target.port,
                        )
                        if current in visited:
                            raise RemoteImageError(
                                502,
                                "remote_image_redirect_invalid",
                                "The image redirect chain is invalid",
                            )
                        visited.add(current)

                        async with session.get(
                            current,
                            allow_redirects=False,
                        ) as response:
                            if response.status in _REDIRECT_STATUSES:
                                if redirect_count >= MAX_REMOTE_IMAGE_REDIRECTS:
                                    raise RemoteImageError(
                                        502,
                                        "remote_image_redirect_limit",
                                        "The image redirected too many times",
                                    )
                                location = response.headers.get("Location")
                                if not location:
                                    raise RemoteImageError(
                                        502,
                                        "remote_image_redirect_invalid",
                                        "The image redirect was invalid",
                                    )
                                current = urljoin(current, location)
                                # Validate before the next iteration so an
                                # unsafe redirect fails before another request.
                                validate_remote_image_url(current)
                                continue

                            if response.status != 200:
                                raise RemoteImageError(
                                    502,
                                    "remote_image_upstream_failed",
                                    "The image host did not return a usable response",
                                )

                            content_type = (
                                response.headers.get("Content-Type", "")
                                .split(";", 1)[0]
                                .strip()
                                .lower()
                            )
                            if (
                                content_type == "image/svg+xml"
                                or content_type.startswith("text/")
                                or content_type
                                in {
                                    "application/json",
                                    "application/xml",
                                    "application/xhtml+xml",
                                }
                            ):
                                raise RemoteImageError(
                                    415,
                                    "remote_image_format_unsupported",
                                    "The remote response was not a supported raster image",
                                )

                            raw_length = response.headers.get("Content-Length")
                            if raw_length:
                                try:
                                    content_length = int(raw_length)
                                except ValueError:
                                    content_length = -1
                                if content_length > MAX_REMOTE_IMAGE_BYTES:
                                    raise RemoteImageError(
                                        413,
                                        "remote_image_too_large",
                                        "The image response is too large",
                                    )

                            body = bytearray()
                            async for chunk in response.content.iter_chunked(64 * 1024):
                                body.extend(chunk)
                                if len(body) > MAX_REMOTE_IMAGE_BYTES:
                                    raise RemoteImageError(
                                        413,
                                        "remote_image_too_large",
                                        "The image response is too large",
                                    )
                            return await asyncio.to_thread(
                                validate_remote_image_bytes,
                                bytes(body),
                            )

                    raise RemoteImageError(
                        502,
                        "remote_image_redirect_limit",
                        "The image redirected too many times",
                    )
    except RemoteImageError:
        raise
    except (TimeoutError, asyncio.TimeoutError) as exc:
        raise RemoteImageError(
            504,
            "remote_image_timeout",
            "The image request timed out",
        ) from exc
    except (aiohttp.ClientError, OSError) as exc:
        raise RemoteImageError(
            502,
            "remote_image_fetch_failed",
            "The image could not be fetched",
        ) from exc
    finally:
        await active_resolver.close()
