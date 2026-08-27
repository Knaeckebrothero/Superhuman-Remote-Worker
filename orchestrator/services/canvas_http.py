"""One-shot HTTP/1.1 transport over a Canvas SSH direct channel.

Only the authenticated SSH transport is pooled. Each call here owns one
reader/writer pair, sends one completely spooled request with
``Connection: close``, incrementally parses one response, and closes the pair.
SSE and WebSocket are deliberately rejected by the policy layer in this slice.
"""

from __future__ import annotations

import asyncio
import inspect
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from tempfile import SpooledTemporaryFile
from typing import Any, AsyncIterable, AsyncIterator, BinaryIO

import h11

from services.canvas_proxy_policy import (
    CanvasProxyError,
    CanvasProxyLimits,
    CanvasPublicOrigin,
    CanvasUpstreamRequest,
    sanitize_canvas_response_headers,
)

_READ_SIZE = 64 * 1024
_SPOOL_MEMORY_BYTES = 256 * 1024


@dataclass(frozen=True, slots=True)
class _ResponsePreamble:
    status_code: int
    content_length: int | None


class CanvasRequestBody:
    """A private bounded spool which must exist before upstream I/O begins."""

    __slots__ = ("_closed", "_spool", "size")

    def __init__(self, spool: BinaryIO, size: int) -> None:
        self._spool = spool
        self.size = size
        self._closed = False

    async def iter_chunks(self) -> AsyncIterator[bytes]:
        if self._closed:
            raise RuntimeError("Canvas request spool is closed")
        self._spool.seek(0)
        while True:
            chunk = self._spool.read(_READ_SIZE)
            if not chunk:
                break
            yield chunk

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._spool.close()

    def __enter__(self) -> "CanvasRequestBody":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


async def spool_canvas_request_body(
    chunks: AsyncIterable[bytes],
    *,
    declared_content_length: int | None,
    limits: CanvasProxyLimits = CanvasProxyLimits(),
) -> CanvasRequestBody:
    """Decode-independent bounded spooling before a direct channel is opened.

    ASGI/edge framing has already decoded any HTTP transfer coding. Content
    coding remains opaque application data. The caller passes the validated
    single Content-Length, if present, and later binds the returned exact size
    through ``ValidatedCanvasRequest.prepare_upstream``.
    """

    if (
        declared_content_length is not None
        and declared_content_length > limits.max_request_body_bytes
    ):
        raise CanvasProxyError(
            413, "canvas_request_too_large", "Canvas request body is too large"
        )
    spool = SpooledTemporaryFile(max_size=_SPOOL_MEMORY_BYTES, mode="w+b")
    total = 0
    iterator = chunks.__aiter__()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + limits.exchange_timeout_seconds
    try:
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise CanvasProxyError(
                    408, "canvas_request_timeout", "Canvas request body timed out"
                )
            try:
                async with asyncio.timeout(min(limits.idle_timeout_seconds, remaining)):
                    chunk = await anext(iterator)
            except StopAsyncIteration:
                break
            except TimeoutError as exc:
                raise CanvasProxyError(
                    408, "canvas_request_timeout", "Canvas request body timed out"
                ) from exc
            if not isinstance(chunk, bytes | bytearray | memoryview):
                raise TypeError("Canvas request chunks must be bytes-like")
            raw = bytes(chunk)
            total += len(raw)
            if total > limits.max_request_body_bytes:
                raise CanvasProxyError(
                    413,
                    "canvas_request_too_large",
                    "Canvas request body is too large",
                )
            if raw:
                spool.write(raw)
        if declared_content_length is not None and total != declared_content_length:
            raise CanvasProxyError(
                400,
                "canvas_request_framing_invalid",
                "Content-Length does not match the decoded request body",
            )
        spool.seek(0)
        return CanvasRequestBody(spool, total)
    except BaseException:
        spool.close()
        raise


async def _writer_send(writer: Any, data: bytes, timeout: float) -> None:
    if not data:
        return
    writer.write(data)
    drain = getattr(writer, "drain", None)
    if callable(drain):
        result = drain()
        if inspect.isawaitable(result):
            try:
                async with asyncio.timeout(timeout):
                    await result
            except TimeoutError as exc:
                raise CanvasProxyError(
                    504,
                    "canvas_upstream_timeout",
                    "Workspace application write timed out",
                ) from exc


async def _close_writer(writer: Any, *, timeout: float) -> None:
    close = getattr(writer, "close", None)
    if callable(close):
        close()
    wait_closed = getattr(writer, "wait_closed", None)
    if callable(wait_closed):
        with suppress(Exception):
            result = wait_closed()
            if inspect.isawaitable(result):
                async with asyncio.timeout(max(0.05, timeout)):
                    await result


def _validate_raw_response_preamble(
    head: bytes,
    *,
    request_method: str,
    limits: CanvasProxyLimits,
) -> _ResponsePreamble:
    """Strict structural guard before h11's single semantic parser boundary.

    h11 intentionally coalesces identical Content-Length fields and follows RFC
    precedence when both Transfer-Encoding and Content-Length occur. Canvas is
    stricter, so this narrow raw preflight rejects those forms before h11 sees
    or forwards any response metadata.
    """

    if len(head) + 4 > limits.max_header_bytes:
        raise CanvasProxyError(
            502, "canvas_upstream_headers_too_large", "Upstream headers are too large"
        )
    if b"\n" in head.replace(b"\r\n", b""):
        raise CanvasProxyError(
            502, "canvas_upstream_invalid", "Upstream headers use invalid line endings"
        )
    lines = head.split(b"\r\n")
    if not lines or len(lines) - 1 > limits.max_header_fields:
        raise CanvasProxyError(
            502, "canvas_upstream_headers_too_large", "Upstream has too many headers"
        )
    status_parts = lines[0].split(b" ", 2)
    if (
        len(status_parts) < 2
        or status_parts[0] not in {b"HTTP/1.0", b"HTTP/1.1"}
        or len(status_parts[1]) != 3
        or not status_parts[1].isdigit()
    ):
        raise CanvasProxyError(
            502, "canvas_upstream_invalid", "Upstream status line is invalid"
        )
    status_code = int(status_parts[1])
    if not 100 <= status_code <= 599:
        raise CanvasProxyError(
            502, "canvas_upstream_invalid", "Upstream status code is invalid"
        )
    if status_code < 200:
        raise CanvasProxyError(
            502,
            "canvas_protocol_switch_rejected",
            "Informational and switching responses are not supported",
        )

    lengths: list[bytes] = []
    transfers: list[bytes] = []
    connections: list[bytes] = []
    for line in lines[1:]:
        if not line or line[:1] in {b" ", b"\t"}:
            raise CanvasProxyError(
                502, "canvas_upstream_invalid", "Upstream header line is invalid"
            )
        name, separator, value = line.partition(b":")
        if not separator or not name or any(byte <= 32 or byte >= 127 for byte in name):
            raise CanvasProxyError(
                502, "canvas_upstream_invalid", "Upstream header name is invalid"
            )
        lower = name.lower()
        value = value.strip()
        if lower == b"content-length":
            lengths.append(value)
        elif lower == b"transfer-encoding":
            transfers.append(value.lower())
        elif lower == b"trailer":
            raise CanvasProxyError(
                502, "canvas_upstream_invalid", "Upstream trailers are not supported"
            )
        elif lower == b"upgrade":
            raise CanvasProxyError(
                502,
                "canvas_protocol_switch_rejected",
                "Upstream protocol switch is not supported",
            )
        elif lower == b"connection":
            connections.append(value.lower())

    if len(lengths) > 1 or (lengths and transfers) or len(transfers) > 1:
        raise CanvasProxyError(
            502, "canvas_upstream_framing_invalid", "Upstream framing is ambiguous"
        )
    content_length: int | None = None
    if lengths:
        if not lengths[0].isdigit() or b"," in lengths[0] or len(lengths[0]) > 20:
            raise CanvasProxyError(
                502, "canvas_upstream_framing_invalid", "Upstream length is invalid"
            )
        content_length = int(lengths[0])
    if transfers and transfers[0] != b"chunked":
        raise CanvasProxyError(
            502,
            "canvas_upstream_framing_invalid",
            "Unsupported upstream transfer coding",
        )
    if status_code == 204 and (lengths or transfers):
        raise CanvasProxyError(
            502,
            "canvas_upstream_framing_invalid",
            "A 204 upstream response cannot carry body framing",
        )
    if status_code == 304 and transfers:
        raise CanvasProxyError(
            502,
            "canvas_upstream_framing_invalid",
            "A 304 upstream response cannot use transfer coding",
        )
    if any(
        b"upgrade" in {token.strip() for token in value.split(b",")}
        for value in connections
    ):
        raise CanvasProxyError(
            502,
            "canvas_protocol_switch_rejected",
            "Upstream protocol switch is not supported",
        )
    body_is_semantic = request_method != "HEAD" and status_code not in {204, 304}
    if (
        body_is_semantic
        and content_length is not None
        and content_length > limits.max_response_body_bytes
    ):
        raise CanvasProxyError(
            502,
            "canvas_response_too_large",
            "Workspace application response is too large",
        )
    return _ResponsePreamble(status_code=status_code, content_length=content_length)


async def _read_response_head(
    reader: Any,
    *,
    request_method: str,
    limits: CanvasProxyLimits,
    deadline: float,
) -> tuple[bytes, _ResponsePreamble]:
    buffered = bytearray()
    while True:
        marker = buffered.find(b"\r\n\r\n")
        if marker >= 0:
            preamble = _validate_raw_response_preamble(
                bytes(buffered[:marker]),
                request_method=request_method,
                limits=limits,
            )
            return bytes(buffered), preamble
        if len(buffered) > limits.max_header_bytes:
            raise CanvasProxyError(
                502,
                "canvas_upstream_headers_too_large",
                "Upstream headers are too large",
            )
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise CanvasProxyError(
                504, "canvas_upstream_timeout", "Workspace application timed out"
            )
        try:
            async with asyncio.timeout(min(limits.idle_timeout_seconds, remaining)):
                data = await reader.read(_READ_SIZE)
        except TimeoutError as exc:
            raise CanvasProxyError(
                504, "canvas_upstream_timeout", "Workspace application timed out"
            ) from exc
        if not data:
            raise CanvasProxyError(
                502,
                "canvas_upstream_invalid",
                "Workspace application closed before sending response headers",
            )
        buffered.extend(data)


class CanvasHTTPResponse:
    """Sanitized response metadata plus a single-consumer streaming body."""

    __slots__ = (
        "_closed",
        "_connection",
        "_consumed",
        "_deadline",
        "_limits",
        "_reader",
        "_writer",
        "headers",
        "status_code",
    )

    def __init__(
        self,
        *,
        status_code: int,
        headers: tuple[tuple[bytes, bytes], ...],
        connection: h11.Connection,
        reader: Any,
        writer: Any,
        limits: CanvasProxyLimits,
        deadline: float,
    ) -> None:
        self.status_code = status_code
        self.headers = headers
        self._connection = connection
        self._reader = reader
        self._writer = writer
        self._limits = limits
        self._deadline = deadline
        self._consumed = False
        self._closed = False

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await _close_writer(self._writer, timeout=self._limits.connect_timeout_seconds)

    async def aiter_bytes(self) -> AsyncIterator[bytes]:
        if self._closed:
            raise RuntimeError("Canvas response channel is closed")
        if self._consumed:
            raise RuntimeError("Canvas response body can be consumed only once")
        self._consumed = True
        total = 0
        try:
            while True:
                if self._closed:
                    raise RuntimeError("Canvas response channel is closed")
                try:
                    event = self._connection.next_event()
                except h11.RemoteProtocolError as exc:
                    raise CanvasProxyError(
                        502,
                        "canvas_upstream_framing_invalid",
                        "Workspace application sent invalid HTTP framing",
                    ) from exc
                if isinstance(event, h11.Data):
                    total += len(event.data)
                    if total > self._limits.max_response_body_bytes:
                        raise CanvasProxyError(
                            502,
                            "canvas_response_too_large",
                            "Workspace application response is too large",
                        )
                    if event.data:
                        yield bytes(event.data)
                    continue
                if isinstance(event, h11.EndOfMessage):
                    if event.headers or self._connection.trailing_data[0]:
                        raise CanvasProxyError(
                            502,
                            "canvas_upstream_framing_invalid",
                            "Workspace application sent forbidden trailing data",
                        )
                    return
                if event is h11.NEED_DATA:
                    remaining = self._deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        raise CanvasProxyError(
                            504,
                            "canvas_upstream_timeout",
                            "Workspace application response timed out",
                        )
                    try:
                        async with asyncio.timeout(
                            min(self._limits.idle_timeout_seconds, remaining)
                        ):
                            data = await self._reader.read(_READ_SIZE)
                    except TimeoutError as exc:
                        raise CanvasProxyError(
                            504,
                            "canvas_upstream_timeout",
                            "Workspace application response timed out",
                        ) from exc
                    try:
                        self._connection.receive_data(data)
                    except h11.RemoteProtocolError as exc:
                        raise CanvasProxyError(
                            502,
                            "canvas_upstream_framing_invalid",
                            "Workspace application sent invalid HTTP framing",
                        ) from exc
                    continue
                raise CanvasProxyError(
                    502,
                    "canvas_upstream_framing_invalid",
                    "Workspace application response did not terminate cleanly",
                )
        finally:
            await self.aclose()


@asynccontextmanager
async def open_canvas_http_exchange(
    *,
    reader: Any,
    writer: Any,
    request: CanvasUpstreamRequest,
    body: CanvasRequestBody,
    public_origin: CanvasPublicOrigin,
    cockpit_origins: tuple[str, ...],
    entry_port: int,
    limits: CanvasProxyLimits = CanvasProxyLimits(),
) -> AsyncIterator[CanvasHTTPResponse]:
    """Send and stream exactly one ordinary HTTP exchange.

    ``reader`` and ``writer`` are the request-scoped AsyncSSH direct-channel
    streams supplied by ``PinnedSSHTransportPool.open_loopback_connection``.
    Channel creation remains outside this module so generation/session
    revalidation and registry cancellation can wrap the whole exchange.
    """

    if body.size != request.body_size:
        raise ValueError("Prepared request and body spool sizes differ")
    connection = h11.Connection(
        our_role=h11.CLIENT,
        max_incomplete_event_size=limits.max_header_bytes,
    )
    response: CanvasHTTPResponse | None = None
    loop = asyncio.get_running_loop()
    deadline = loop.time() + limits.exchange_timeout_seconds

    def send_timeout() -> float:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise CanvasProxyError(
                504,
                "canvas_upstream_timeout",
                "Workspace application request timed out",
            )
        return min(limits.idle_timeout_seconds, remaining)

    try:
        try:
            await _writer_send(
                writer,
                connection.send(
                    h11.Request(
                        method=request.method,
                        target=request.target,
                        headers=list(request.headers),
                    )
                ),
                send_timeout(),
            )
            async for chunk in body.iter_chunks():
                await _writer_send(
                    writer,
                    connection.send(h11.Data(data=chunk)),
                    send_timeout(),
                )
            await _writer_send(
                writer,
                connection.send(h11.EndOfMessage()),
                send_timeout(),
            )
        except (h11.LocalProtocolError, OSError, ConnectionError) as exc:
            raise CanvasProxyError(
                502,
                "canvas_upstream_unavailable",
                "Workspace application request could not be sent",
            ) from exc

        initial, preamble = await _read_response_head(
            reader,
            request_method=request.method.decode("ascii"),
            limits=limits,
            deadline=deadline,
        )
        try:
            connection.receive_data(initial)
            event = connection.next_event()
        except h11.RemoteProtocolError as exc:
            raise CanvasProxyError(
                502,
                "canvas_upstream_framing_invalid",
                "Workspace application sent invalid HTTP headers",
            ) from exc
        if (
            not isinstance(event, h11.Response)
            or event.status_code != preamble.status_code
        ):
            raise CanvasProxyError(
                502,
                "canvas_upstream_invalid",
                "Workspace application sent an invalid response",
            )
        request_path = request.target.partition(b"?")[0].decode("ascii")
        sanitized = sanitize_canvas_response_headers(
            status_code=event.status_code,
            headers=list(event.headers),
            request_method=request.method.decode("ascii"),
            request_path=request_path,
            public_origin=public_origin,
            cockpit_origins=cockpit_origins,
            entry_port=entry_port,
            limits=limits,
        )
        response = CanvasHTTPResponse(
            status_code=sanitized.status_code,
            headers=sanitized.headers,
            connection=connection,
            reader=reader,
            writer=writer,
            limits=limits,
            deadline=deadline,
        )
        yield response
    finally:
        body.close()
        if response is not None:
            await response.aclose()
        else:
            await _close_writer(writer, timeout=limits.connect_timeout_seconds)


__all__ = [
    "CanvasHTTPResponse",
    "CanvasRequestBody",
    "open_canvas_http_exchange",
    "spool_canvas_request_body",
]
