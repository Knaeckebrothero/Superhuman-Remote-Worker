"""Raw fake-stream tests for the Canvas one-shot h11 transport."""

from __future__ import annotations

import asyncio
from collections import deque
from typing import AsyncIterator

import pytest

from orchestrator.services.canvas_http import (
    open_canvas_http_exchange,
    spool_canvas_request_body,
)
from orchestrator.services.canvas_proxy_policy import (
    CanvasProxyError,
    CanvasProxyLimits,
    CanvasPublicOrigin,
    validate_canvas_request,
)

ORIGIN = CanvasPublicOrigin("11111111-aaaa-4aaa-8aaa-111111111111.canvas.test")
COCKPIT_ORIGINS = ("https://cockpit.test",)


class FakeReader:
    def __init__(self, *chunks: bytes) -> None:
        self.chunks = deque(chunks)
        self.calls = 0

    async def read(self, _: int) -> bytes:
        self.calls += 1
        return self.chunks.popleft() if self.chunks else b""


class FakeWriter:
    def __init__(self) -> None:
        self.data = bytearray()
        self.closed = False
        self.drains = 0

    def write(self, data: bytes) -> None:
        self.data.extend(data)

    async def drain(self) -> None:
        self.drains += 1

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


class StallingCloseWriter(FakeWriter):
    async def wait_closed(self) -> None:
        await asyncio.Event().wait()


async def _chunks(*values: bytes) -> AsyncIterator[bytes]:
    for value in values:
        yield value


def _validated_request(method: str = "GET", length: int | None = None):
    headers = [(b"host", ORIGIN.authority.encode("ascii"))]
    if method not in {"GET", "HEAD"}:
        headers.append((b"origin", ORIGIN.origin.encode("ascii")))
    if length is not None:
        headers.append((b"content-length", str(length).encode("ascii")))
    return validate_canvas_request(
        method=method,
        raw_path=b"/submit" if method == "POST" else b"/",
        raw_query=b"a=1&a=",
        headers=headers,
        public_origin=ORIGIN,
    )


async def _open(
    raw_response: bytes,
    *,
    method: str = "GET",
    body_chunks: tuple[bytes, ...] = (),
    limits: CanvasProxyLimits = CanvasProxyLimits(),
):
    validated = _validated_request(method, sum(map(len, body_chunks)))
    body = await spool_canvas_request_body(
        _chunks(*body_chunks),
        declared_content_length=validated.declared_content_length,
        limits=limits,
    )
    reader = FakeReader(raw_response)
    writer = FakeWriter()
    return validated, body, reader, writer


@pytest.mark.asyncio
async def test_spools_complete_bounded_body_before_exchange() -> None:
    limits = CanvasProxyLimits(max_request_body_bytes=4)

    with pytest.raises(CanvasProxyError) as caught:
        await spool_canvas_request_body(
            _chunks(b"abc", b"de"), declared_content_length=None, limits=limits
        )

    assert caught.value.status_code == 413


@pytest.mark.asyncio
async def test_h11_exchange_reframes_request_and_streams_chunked_response() -> None:
    validated, body, reader, writer = await _open(
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: text/plain\r\n"
        b"Transfer-Encoding: chunked\r\n"
        b"Set-Cookie: app=secret\r\n"
        b"\r\n"
        b"5\r\nworld\r\n0\r\n\r\n",
        method="POST",
        body_chunks=(b"hel", b"lo"),
    )
    try:
        async with open_canvas_http_exchange(
            reader=reader,
            writer=writer,
            request=validated.prepare_upstream(body.size),
            body=body,
            public_origin=ORIGIN,
            cockpit_origins=COCKPIT_ORIGINS,
            entry_port=8501,
        ) as response:
            assert response.status_code == 200
            response_headers = dict(response.headers)
            assert b"set-cookie" not in response_headers
            assert response_headers[b"cache-control"] == b"private, no-store"
            assert (
                b"frame-ancestors 'self' https://cockpit.test"
                in response_headers[b"content-security-policy"]
            )
            assert [chunk async for chunk in response.aiter_bytes()] == [b"world"]
    finally:
        body.close()

    upstream = bytes(writer.data)
    assert upstream.startswith(b"POST /submit?a=1&a= HTTP/1.1\r\n")
    assert upstream.count(b"content-length: 5\r\n") == 1
    assert b"transfer-encoding" not in upstream.lower()
    assert b"authorization" not in upstream.lower()
    assert b"x-forwarded-for" not in upstream.lower()
    assert upstream.endswith(b"\r\n\r\nhello")
    assert writer.closed


@pytest.mark.asyncio
async def test_h11_exchange_bounds_stalled_channel_teardown() -> None:
    limits = CanvasProxyLimits(connect_timeout_seconds=0.01)
    validated = _validated_request()
    body = await spool_canvas_request_body(
        _chunks(), declared_content_length=None, limits=limits
    )
    writer = StallingCloseWriter()
    try:
        async with asyncio.timeout(0.5):
            async with open_canvas_http_exchange(
                reader=FakeReader(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"),
                writer=writer,
                request=validated.prepare_upstream(0),
                body=body,
                public_origin=ORIGIN,
                cockpit_origins=COCKPIT_ORIGINS,
                entry_port=8501,
                limits=limits,
            ) as response:
                assert [chunk async for chunk in response.aiter_bytes()] == []
    finally:
        body.close()
    assert writer.closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw_response", "code"),
    [
        (
            b"HTTP/1.1 200 OK\r\nContent-Length: 1\r\nContent-Length: 1\r\n\r\nx",
            "canvas_upstream_framing_invalid",
        ),
        (
            b"HTTP/1.1 200 OK\r\nContent-Length: 1\r\n"
            b"Transfer-Encoding: chunked\r\n\r\n1\r\nx\r\n0\r\n\r\n",
            "canvas_upstream_framing_invalid",
        ),
        (
            b"HTTP/1.1 200 OK\nContent-Length: 1\n\nx",
            "canvas_upstream_invalid",
        ),
        (
            b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n\r\n",
            "canvas_protocol_switch_rejected",
        ),
        (
            b"HTTP/1.1 200 OK\r\nTrailer: digest\r\n"
            b"Transfer-Encoding: chunked\r\n\r\n0\r\nDigest: x\r\n\r\n",
            "canvas_upstream_invalid",
        ),
        (
            b"HTTP/1.1 204 No Content\r\nContent-Length: 1\r\n\r\n",
            "canvas_upstream_framing_invalid",
        ),
    ],
)
async def test_h11_exchange_rejects_raw_framing_ambiguity_before_response(
    raw_response: bytes, code: str
) -> None:
    validated, body, reader, writer = await _open(raw_response)
    try:
        with pytest.raises(CanvasProxyError) as caught:
            async with open_canvas_http_exchange(
                reader=reader,
                writer=writer,
                request=validated.prepare_upstream(body.size),
                body=body,
                public_origin=ORIGIN,
                cockpit_origins=COCKPIT_ORIGINS,
                entry_port=8501,
            ):
                pass
        assert caught.value.code == code
        assert writer.closed
    finally:
        body.close()


@pytest.mark.asyncio
async def test_h11_exchange_rejects_sse_before_exposing_response() -> None:
    validated, body, reader, writer = await _open(
        b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\n"
    )
    try:
        with pytest.raises(CanvasProxyError) as caught:
            async with open_canvas_http_exchange(
                reader=reader,
                writer=writer,
                request=validated.prepare_upstream(body.size),
                body=body,
                public_origin=ORIGIN,
                cockpit_origins=COCKPIT_ORIGINS,
                entry_port=8501,
            ):
                pass
        assert caught.value.code == "canvas_streaming_unsupported"
    finally:
        body.close()


@pytest.mark.asyncio
async def test_h11_exchange_enforces_declared_and_streamed_response_limits() -> None:
    limits = CanvasProxyLimits(max_response_body_bytes=3)
    validated, body, reader, writer = await _open(
        b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\n\r\ntest", limits=limits
    )
    try:
        with pytest.raises(CanvasProxyError) as declared:
            async with open_canvas_http_exchange(
                reader=reader,
                writer=writer,
                request=validated.prepare_upstream(body.size),
                body=body,
                public_origin=ORIGIN,
                cockpit_origins=COCKPIT_ORIGINS,
                entry_port=8501,
                limits=limits,
            ):
                pass
        assert declared.value.code == "canvas_response_too_large"
    finally:
        body.close()

    validated, body, reader, writer = await _open(
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n4\r\ntest\r\n0\r\n\r\n",
        limits=limits,
    )
    try:
        async with open_canvas_http_exchange(
            reader=reader,
            writer=writer,
            request=validated.prepare_upstream(body.size),
            body=body,
            public_origin=ORIGIN,
            cockpit_origins=COCKPIT_ORIGINS,
            entry_port=8501,
            limits=limits,
        ) as response:
            with pytest.raises(CanvasProxyError) as streamed:
                _ = [chunk async for chunk in response.aiter_bytes()]
            assert streamed.value.code == "canvas_response_too_large"
    finally:
        body.close()


@pytest.mark.asyncio
async def test_h11_exchange_streams_first_body_bytes_before_later_reads() -> None:
    validated = _validated_request()
    body = await spool_canvas_request_body(
        _chunks(), declared_content_length=validated.declared_content_length
    )
    reader = FakeReader(
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nfirst\r\n",
        b"6\r\nsecond\r\n0\r\n\r\n",
    )
    writer = FakeWriter()
    try:
        async with open_canvas_http_exchange(
            reader=reader,
            writer=writer,
            request=validated.prepare_upstream(body.size),
            body=body,
            public_origin=ORIGIN,
            cockpit_origins=COCKPIT_ORIGINS,
            entry_port=8501,
        ) as response:
            stream = response.aiter_bytes()
            assert await anext(stream) == b"first"
            assert reader.calls == 1
            assert await anext(stream) == b"second"
            with pytest.raises(StopAsyncIteration):
                await anext(stream)
    finally:
        body.close()


@pytest.mark.asyncio
async def test_h11_exchange_closes_on_invalid_chunk_and_forbidden_trailing_data() -> (
    None
):
    for raw in (
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\nZ\r\nbad\r\n",
        b"HTTP/1.1 204 No Content\r\n\r\nforbidden",
    ):
        validated, body, reader, writer = await _open(raw)
        try:
            async with open_canvas_http_exchange(
                reader=reader,
                writer=writer,
                request=validated.prepare_upstream(body.size),
                body=body,
                public_origin=ORIGIN,
                cockpit_origins=COCKPIT_ORIGINS,
                entry_port=8501,
            ) as response:
                with pytest.raises(CanvasProxyError) as caught:
                    _ = [chunk async for chunk in response.aiter_bytes()]
                assert caught.value.code == "canvas_upstream_framing_invalid"
            assert writer.closed
        finally:
            body.close()
