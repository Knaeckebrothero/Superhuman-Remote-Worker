"""Validation and bounded health checks for Dynamic Canvas live applications.

This Slice-3A service intentionally stops at publication and status. It opens a
request-scoped SSH ``direct-tcpip`` channel to one fixed workspace-loopback
port, verifies that TCP accepts a connection, and closes it. It does not expose
a listener or implement the isolated-origin HTTP proxy.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal
from urllib.parse import quote, unquote_to_bytes
from uuid import UUID

from orchestrator.services import resolve_ssh_key_path
from orchestrator.services.canvas import CanvasRecord, CanvasStatus, WorkspaceAppSource
from orchestrator.services.canvas_ssh import (
    PINNED_SSH_TRANSPORT_POOL,
    CanvasDirectChannelUnavailable,
    CanvasSSHError,
    PinnedSSHTransportPool,
    RemoteWorkspaceTarget,
    bound_workspace_generation,
    resolve_remote_workspace_target,
)

logger = logging.getLogger(__name__)

MIN_CANVAS_APP_PORT = 1024
MAX_CANVAS_APP_PORT = 65535
CANVAS_APP_FIXED_DENIED_PORTS = frozenset({30022, 9222, 38080})
CANVAS_APP_DENYLIST_ENV = "CANVAS_LIVE_PREVIEW_DENIED_PORTS"
_DENYLIST_ENV_ALIASES = (
    CANVAS_APP_DENYLIST_ENV,
    "CANVAS_LIVE_PREVIEW_DENY_PORTS",
    "CANVAS_APP_DENIED_PORTS",
)
_TRUTHY = frozenset({"1", "true", "yes", "on"})
_PERCENT_ESCAPE = re.compile(r"%[0-9A-Fa-f]{2}")
_MALFORMED_PERCENT = re.compile(r"%(?![0-9A-Fa-f]{2})")
MAX_CANVAS_APP_HEALTH_CHECKS = max(
    1, int(os.getenv("CANVAS_MAX_APP_HEALTH_CHECKS", "16"))
)
CANVAS_APP_HEALTH_QUEUE_TIMEOUT = max(
    0.05, float(os.getenv("CANVAS_APP_HEALTH_QUEUE_TIMEOUT", "2"))
)
_APP_HEALTH_SEMAPHORE = asyncio.Semaphore(MAX_CANVAS_APP_HEALTH_CHECKS)

ThreadLoader = Callable[[str], Awaitable[dict[str, Any] | None]]
KeyPathResolver = Callable[[], str | Awaitable[str]]


class CanvasAppError(Exception):
    """Typed live-app validation failure for HTTP and tool adapters."""

    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class ValidatedCanvasApp:
    """A normalized app source plus its bounded publication-time health."""

    source: WorkspaceAppSource
    status: Literal["ready", "starting"]
    _target: RemoteWorkspaceTarget = field(repr=False)


def canvas_live_preview_enabled() -> bool:
    """Return the explicit, default-off live-preview capability gate."""

    return os.getenv("CANVAS_LIVE_PREVIEW_ENABLED", "").strip().lower() in _TRUTHY


def _configured_denied_ports() -> frozenset[int]:
    denied = set(CANVAS_APP_FIXED_DENIED_PORTS)
    for name in _DENYLIST_ENV_ALIASES:
        raw = os.getenv(name, "").strip()
        if not raw:
            continue
        for item in raw.split(","):
            candidate = item.strip()
            if not candidate or not candidate.isascii() or not candidate.isdecimal():
                raise CanvasAppError(
                    503,
                    "canvas_configuration_invalid",
                    f"{name} contains an invalid port",
                )
            port = int(candidate)
            if not 1 <= port <= MAX_CANVAS_APP_PORT:
                raise CanvasAppError(
                    503,
                    "canvas_configuration_invalid",
                    f"{name} contains an invalid port",
                )
            denied.add(port)
    return frozenset(denied)


def validate_workspace_port(port: int) -> int:
    """Validate one agent-selected application port, including denylist policy."""

    if isinstance(port, bool) or not isinstance(port, int):
        raise CanvasAppError(
            422,
            "invalid_canvas_port",
            "Canvas application port must be an integer from 1024 through 65535",
        )
    if not MIN_CANVAS_APP_PORT <= port <= MAX_CANVAS_APP_PORT:
        raise CanvasAppError(
            422,
            "invalid_canvas_port",
            "Canvas application port must be an integer from 1024 through 65535",
        )
    if port in _configured_denied_ports():
        raise CanvasAppError(
            422,
            "canvas_port_reserved",
            "Canvas application port is reserved",
        )
    return port


def canonical_canvas_app_path(path: str) -> str:
    """Apply the normative Canvas origin-form path canonicalization algorithm."""

    if not isinstance(path, str) or not path or len(path) > 2048:
        raise CanvasAppError(
            422, "invalid_canvas_entry_path", "Canvas application path is invalid"
        )
    try:
        raw = path.encode("ascii")
    except UnicodeEncodeError as exc:
        raise CanvasAppError(
            422,
            "invalid_canvas_entry_path",
            "Non-ASCII path characters must be percent-encoded UTF-8",
        ) from exc
    if (
        not raw.startswith(b"/")
        or raw.startswith(b"//")
        or b"?" in raw
        or b"#" in raw
        or b"\\" in raw
        or any(byte < 32 or byte == 127 for byte in raw)
        or _MALFORMED_PERCENT.search(path) is not None
    ):
        raise CanvasAppError(
            422,
            "invalid_canvas_entry_path",
            "Canvas application path must be one canonical absolute origin-form path",
        )

    raw_segments = path.split("/")
    trailing_slash = len(raw_segments) > 1 and raw_segments[-1] == ""
    if path != "/" and any(segment == "" for segment in raw_segments[1:-1]):
        raise CanvasAppError(
            422,
            "invalid_canvas_entry_path",
            "Canvas application path may not contain repeated slashes",
        )

    encoded_segments: list[str] = []
    decoded_segments: list[str] = []
    for raw_segment in raw_segments[1 : -1 if trailing_slash else None]:
        try:
            decoded = unquote_to_bytes(raw_segment).decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise CanvasAppError(
                422,
                "invalid_canvas_entry_path",
                "Canvas application path contains invalid encoded UTF-8",
            ) from exc
        if (
            decoded in {".", ".."}
            or "/" in decoded
            or "\\" in decoded
            or any(ord(char) < 32 or ord(char) == 127 for char in decoded)
            or _PERCENT_ESCAPE.search(decoded) is not None
        ):
            raise CanvasAppError(
                422,
                "invalid_canvas_entry_path",
                "Canvas application path contains unsafe structural characters",
            )
        decoded_segments.append(decoded)
        encoded_segments.append(quote(decoded, safe="-._~", encoding="utf-8"))

    if any(segment.lower() == "_canvas" for segment in decoded_segments):
        raise CanvasAppError(
            422,
            "invalid_canvas_entry_path",
            "Canvas application path uses a reserved control segment",
        )

    canonical = "/" + "/".join(encoded_segments)
    if trailing_slash and canonical != "/":
        canonical += "/"
    if len(canonical) > 2048:
        raise CanvasAppError(
            422,
            "invalid_canvas_entry_path",
            "Canonical Canvas application path exceeds 2048 characters",
        )
    return canonical


class ThreadWorkspaceAppGateway:
    """Validate a bound remote workspace port without publishing a proxy."""

    def __init__(
        self,
        *,
        thread_loader: ThreadLoader | None = None,
        transport_pool: PinnedSSHTransportPool | None = None,
        key_path_resolver: KeyPathResolver | None = None,
        connect_timeout: float = 5.0,
        health_semaphore: asyncio.Semaphore | None = None,
        queue_timeout: float = CANVAS_APP_HEALTH_QUEUE_TIMEOUT,
    ) -> None:
        self._thread_loader = thread_loader
        self._transport_pool = transport_pool or PINNED_SSH_TRANSPORT_POOL
        self._key_path_resolver = key_path_resolver or resolve_ssh_key_path
        self._connect_timeout = max(0.05, connect_timeout)
        self._health_semaphore = health_semaphore or _APP_HEALTH_SEMAPHORE
        self._queue_timeout = max(0.01, queue_timeout)

    async def _authoritative_thread(self, thread: dict[str, Any]) -> dict[str, Any]:
        if self._thread_loader is None:
            return thread
        thread_id = str(thread.get("id") or "")
        fresh = await self._thread_loader(thread_id)
        if fresh is None:
            raise CanvasAppError(404, "workspace_unavailable", "Thread not found")
        return fresh

    async def _key_path(self) -> str:
        result = self._key_path_resolver()
        if inspect.isawaitable(result):
            result = await result
        if not isinstance(result, str) or not result.strip():
            raise CanvasAppError(
                503,
                "workspace_unavailable",
                "Workspace SSH key is not configured",
            )
        return result.strip()

    @staticmethod
    def _translate_ssh_error(error: CanvasSSHError) -> CanvasAppError:
        return CanvasAppError(error.status_code, error.code, error.message)

    @asynccontextmanager
    async def _health_slot(self):
        acquired = False
        try:
            try:
                await asyncio.wait_for(
                    self._health_semaphore.acquire(), timeout=self._queue_timeout
                )
                acquired = True
            except TimeoutError as exc:
                raise CanvasAppError(
                    503,
                    "canvas_capacity_exhausted",
                    "Canvas application health capacity is currently exhausted",
                ) from exc
            yield
        finally:
            if acquired:
                self._health_semaphore.release()

    async def _health(
        self,
        thread: dict[str, Any],
        *,
        port: int,
        expected_generation: UUID,
    ) -> tuple[Literal["ready", "starting"], RemoteWorkspaceTarget]:
        async with self._health_slot():
            try:
                target = resolve_remote_workspace_target(thread, expected_generation)
                key_path = await self._key_path()

                async def resolve_generation() -> dict[str, Any]:
                    return await self._authoritative_thread(thread)

                async with asyncio.timeout(self._connect_timeout):
                    async with self._transport_pool.open_loopback_connection(
                        target=target,
                        destination_port=port,
                        key_path=key_path,
                        generation_resolver=resolve_generation,
                    ):
                        # TCP acceptance is the complete Slice-3A health contract.
                        pass
            except CanvasDirectChannelUnavailable:
                return "starting", target
            except CanvasSSHError as exc:
                raise self._translate_ssh_error(exc) from exc
            except TimeoutError as exc:
                raise CanvasAppError(
                    503,
                    "workspace_unavailable",
                    "Workspace application health check timed out",
                ) from exc
            except CanvasAppError:
                raise
            except (ConnectionError, OSError) as exc:
                raise CanvasAppError(
                    503,
                    "workspace_unavailable",
                    "Workspace application health check failed",
                ) from exc
            return "ready", target

    async def validate_for_presentation(
        self,
        thread: dict[str, Any],
        port: int,
        *,
        entry_path: str = "/",
    ) -> ValidatedCanvasApp:
        """Normalize, bind, and health-check an agent-selected loopback port."""

        port = validate_workspace_port(port)
        entry_path = canonical_canvas_app_path(entry_path)
        authoritative = await self._authoritative_thread(thread)
        try:
            generation = bound_workspace_generation(authoritative)
        except CanvasSSHError as exc:
            raise self._translate_ssh_error(exc) from exc
        status, target = await self._health(
            authoritative, port=port, expected_generation=generation
        )
        # The direct channel validates generation, endpoint, and fingerprint on
        # both sides of its TCP open. Re-read once more before returning the
        # source which the caller may persist.
        fresh = await self._authoritative_thread(authoritative)
        try:
            fresh_generation = bound_workspace_generation(fresh)
            fresh_target = resolve_remote_workspace_target(fresh, generation)
        except CanvasSSHError as exc:
            raise self._translate_ssh_error(exc) from exc
        if fresh_generation != generation or fresh_target != target:
            raise CanvasAppError(
                409,
                "workspace_generation_changed",
                "The workspace changed while the Canvas application was validated",
            )
        return ValidatedCanvasApp(
            source=WorkspaceAppSource(
                entry_port=port,
                entry_path=entry_path,
                routes=(),
                manifest_path=None,
                manifest_version=None,
                workspace_generation=generation,
            ),
            status=status,
            _target=target,
        )

    def revalidate_for_commit(
        self, thread: dict[str, Any], validated: ValidatedCanvasApp
    ) -> None:
        """Recheck the complete attested target immediately before persistence."""

        try:
            current = resolve_remote_workspace_target(
                thread, validated.source.workspace_generation
            )
        except CanvasSSHError as exc:
            raise self._translate_ssh_error(exc) from exc
        if current != validated._target:
            raise CanvasAppError(
                409,
                "workspace_generation_changed",
                "The workspace endpoint changed before Canvas publication",
            )

    async def status_for_record(
        self, thread: dict[str, Any], record: CanvasRecord
    ) -> CanvasStatus:
        """Derive non-throwing app health for a persisted Canvas record."""

        try:
            source = record.source
            if not isinstance(source, WorkspaceAppSource):
                return "error"
            if validate_workspace_port(source.entry_port) != source.entry_port:
                return "error"
            if canonical_canvas_app_path(source.entry_path) != source.entry_path:
                return "error"
            authoritative = await self._authoritative_thread(thread)
            try:
                generation = bound_workspace_generation(authoritative)
            except CanvasSSHError as exc:
                raise self._translate_ssh_error(exc) from exc
            if generation != source.workspace_generation:
                return "unavailable"
            status, _ = await self._health(
                authoritative,
                port=source.entry_port,
                expected_generation=source.workspace_generation,
            )
            return status
        except asyncio.CancelledError:
            raise
        except CanvasAppError as exc:
            if exc.code in {
                "workspace_unavailable",
                "workspace_generation_changed",
            }:
                return "unavailable"
            return "error"
        except Exception:
            logger.exception(
                "Unexpected Canvas app health failure for thread %s",
                thread.get("id"),
            )
            return "error"


__all__ = [
    "CANVAS_APP_DENYLIST_ENV",
    "CANVAS_APP_FIXED_DENIED_PORTS",
    "CanvasAppError",
    "ThreadWorkspaceAppGateway",
    "ValidatedCanvasApp",
    "canonical_canvas_app_path",
    "canvas_live_preview_enabled",
    "validate_workspace_port",
]
