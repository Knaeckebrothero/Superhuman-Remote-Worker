"""Freeze the sync/async HTTP send boundary before sharing its preflight.

These tests use real httpx clients and MockTransport, with a deterministic
token counter. They characterize request handling, not tokenizer accuracy or
provider behavior. Socket/tokenizer tripwires also fail after caught errors.
"""

import json
import logging
import socket
from contextlib import asynccontextmanager
from unittest.mock import Mock

import httpx
import openai
import pytest

from shared.runtime.llm import reasoning_chat as reasoning
from shared.runtime.llm.key_ring import KeyRing


@pytest.fixture(params=["sync", "async"])
def mode(request):
    return request.param


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    attempts = []

    def forbid(*args, **kwargs):
        attempts.append(True)
        raise AssertionError("unexpected network or tokenizer access")

    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(tmp_path / "empty-token-cache"))
    monkeypatch.setenv("MAX_CONTEXT_TOKENS", "0")
    monkeypatch.delenv("DEBUG_CODEX_RAW_RESPONSE", raising=False)
    monkeypatch.delenv("DEBUG_LLM_STREAM", raising=False)
    monkeypatch.setattr(socket, "getaddrinfo", forbid)
    monkeypatch.setattr(socket.socket, "connect", forbid)
    monkeypatch.setattr(socket.socket, "connect_ex", forbid)
    if reasoning.TIKTOKEN_AVAILABLE:
        monkeypatch.setattr(reasoning.tiktoken, "get_encoding", forbid)
        monkeypatch.setattr(reasoning.tiktoken, "encoding_for_model", forbid)
    yield
    assert not attempts, "an allow-through path swallowed the offline tripwire"


@pytest.fixture(autouse=True)
def counter(monkeypatch):
    spy = Mock(return_value=10)
    monkeypatch.setattr(reasoning, "count_request_tokens", spy)
    return spy


@asynccontextmanager
async def client_for(mode, handler, **kwargs):
    kwargs = {"transport": httpx.MockTransport(handler), "trust_env": False, **kwargs}
    if mode == "sync":
        with reasoning.ReasoningCapturingClient(**kwargs) as client:
            yield client
    else:
        async with reasoning.AsyncReasoningCapturingClient(**kwargs) as client:
            yield client


async def send(mode, client, request, **kwargs):
    if mode == "sync":
        return client.send(request, **kwargs)
    return await client.send(request, **kwargs)


def chat_response():
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "message": {
                        "content": "fixture answer",
                        "reasoning_content": "reason",
                    }
                }
            ]
        },
    )


def request_for(path="/v1/chat/completions", **kwargs):
    kwargs.setdefault("json", {"model": "body-model", "messages": []})
    return httpx.Request(
        "POST",
        "https://fixture.invalid" + path,
        headers={"authorization": "Bearer original-fixture"},
        **kwargs,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "is_chat", "is_responses"),
    [
        ("/v1/chat/completions", True, False),
        ("/v1/chat/completions/", True, False),
        ("/v1/chat/completions?version=1", True, False),
        ("/health?next=/chat/completions", True, False),
        ("/v1/responses", False, True),
        ("/v1/responses/", False, True),
        ("/v1/responses/id", False, True),
        ("/v1/responses?version=1", False, False),
        ("/v1/responses/?version=1", False, True),
        ("/health?next=/responses/id", False, True),
        ("/v1/embeddings", False, False),
        ("/v1/responses-other", False, False),
        ("/health", False, False),
    ],
)
async def test_url_classification_preserves_header_body_and_capture(
    mode, counter, path, is_chat, is_responses
):
    request = request_for(path)
    original_body = request.content
    response = chat_response()
    transport = Mock(return_value=response)
    ring = KeyRing(["fixture-primary"])
    async with client_for(
        mode, transport, key_ring=ring, model="configured-model"
    ) as client:
        result = await send(mode, client, request)
        assert result is response
        assert result.request is request
        assert client._last_reasoning_content == ("reason" if is_chat else None)
    transport.assert_called_once_with(request)
    assert request.content == original_body
    if is_chat or is_responses:
        counter.assert_called_once_with(json.loads(original_body), "configured-model")
        assert request.headers["authorization"] == "Bearer fixture-primary"
    else:
        counter.assert_not_called()
        assert request.headers["authorization"] == "Bearer original-fixture"


@pytest.mark.asyncio
async def test_classification_has_no_method_or_content_type_filter(mode, counter):
    request = httpx.Request(
        "GET",
        "https://fixture.invalid/v1/chat/completions",
        content=b'{"messages":[]}',
        headers={"content-type": "text/plain"},
    )
    transport = Mock(return_value=chat_response())
    async with client_for(
        mode, transport, key_ring=KeyRing(["fixture-primary"])
    ) as client:
        await send(mode, client, request)
    counter.assert_called_once_with({"messages": []}, "gpt-4")
    transport.assert_called_once_with(request)
    assert request.headers["authorization"] == "Bearer fixture-primary"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/v1/chat/completions", "/v1/responses", "/health"])
async def test_exhausted_key_ring_preserves_original_header(
    mode, counter, caplog, path
):
    ring = KeyRing(["fixture-primary"], cooldown_seconds=3600)
    assert ring.rotate("fixture exhaustion") is None
    request = request_for(path)
    transport = Mock(return_value=chat_response())
    caplog.clear()
    async with client_for(mode, transport, key_ring=ring) as client:
        result = await send(mode, client, request)
    assert result.status_code == 200
    assert request.headers["authorization"] == "Bearer original-fixture"
    transport.assert_called_once_with(request)
    is_llm = path != "/health"
    assert counter.call_count == int(is_llm)
    assert (
        "KeyRing: all keys exhausted, sending with original header" in caplog.text
    ) == is_llm


@pytest.mark.asyncio
async def test_only_runtime_error_from_key_selection_is_allowed_through(mode, counter):
    class BrokenRing:
        @property
        def current_key(self):
            raise ValueError("fixture key selection error")

    transport = Mock(return_value=chat_response())
    async with client_for(mode, transport, key_ring=BrokenRing()) as client:
        with pytest.raises(ValueError, match="fixture key selection error"):
            await send(mode, client, request_for())
    counter.assert_not_called()
    transport.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "expected_log", "calls"),
    [
        (b"not json", "Skipping token count for non-JSON request", 0),
        (b"", "Skipping token count for non-JSON request", 0),
        (b"\xff", "Token counting failed, allowing request:", 0),
        (b"null", "Token counting failed, allowing request:", 1),
        (b"[]", "Token counting failed, allowing request:", 1),
        (b'"text"', "Token counting failed, allowing request:", 1),
    ],
)
async def test_malformed_and_non_object_bodies_are_sent_unchanged(
    mode, counter, caplog, body, expected_log, calls
):
    counter.side_effect = lambda parsed, model: parsed.get("count", 10)
    request = httpx.Request(
        "POST", "https://fixture.invalid/v1/responses", content=body
    )
    transport = Mock(return_value=chat_response())
    with caplog.at_level(logging.DEBUG, logger=reasoning.__name__):
        async with client_for(
            mode, transport, key_ring=KeyRing(["fixture-primary"])
        ) as client:
            result = await send(mode, client, request)
    assert result.status_code == 200
    transport.assert_called_once_with(request)
    assert request.content == body
    assert request.headers["authorization"] == "Bearer fixture-primary"
    assert counter.call_count == calls
    assert expected_log in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "expected_log"),
    [
        (
            ValueError("fixture counter unavailable"),
            "Token counting failed, allowing request: fixture counter unavailable",
        ),
        (
            json.JSONDecodeError("fixture", "x", 0),
            "Skipping token count for non-JSON request",
        ),
    ],
)
async def test_counting_errors_keep_the_original_response(
    mode, counter, caplog, failure, expected_log
):
    counter.side_effect = failure
    response = chat_response()
    transport = Mock(return_value=response)
    with caplog.at_level(logging.DEBUG, logger=reasoning.__name__):
        async with client_for(mode, transport) as client:
            result = await send(mode, client, request_for())
            assert client._last_reasoning_content == "reason"
    assert result is response
    assert expected_log in caplog.text
    counter.assert_called_once()
    transport.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("token_count", [90, 91, 100, 101])
async def test_strict_warning_and_overflow_boundaries(
    mode, counter, caplog, token_count
):
    counter.return_value = token_count
    request = request_for()
    transport = Mock(return_value=chat_response())
    with caplog.at_level(logging.WARNING, logger=reasoning.__name__):
        async with client_for(mode, transport, max_context_tokens=100) as client:
            result = await send(mode, client, request)
    messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == reasoning.__name__
    ]
    expected = []
    if token_count > 90:
        expected.append(
            f"Request approaching context limit: {token_count}/100 tokens ({token_count:.1f}%)"
        )
    if token_count > 100:
        expected.append(
            "Context overflow at HTTP layer: 101 tokens exceeds limit of 100"
        )
    assert messages == expected
    assert result.status_code == (413 if token_count > 100 else 200)
    assert transport.call_count == int(token_count <= 100)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/v1/chat/completions", "/v1/responses"])
@pytest.mark.parametrize("stream", [False, True])
async def test_overflow_response_exact_body_identity_and_no_transport(
    mode, counter, monkeypatch, path, stream
):
    counter.return_value = 150_001
    request = request_for(path)
    original_body = request.content
    transport = Mock(return_value=chat_response())
    build_response = Mock(wraps=reasoning._overflow_response_413)
    monkeypatch.setattr(reasoning, "_overflow_response_413", build_response)
    async with client_for(
        mode,
        transport,
        max_context_tokens=100_000,
        key_ring=KeyRing(["fixture-primary"]),
    ) as client:
        result = await send(mode, client, request, stream=stream)
        assert client._last_reasoning_content is None
        assert client._active_stream_tap is None
    expected = {
        "error": {
            "message": "Request body has 150,001 tokens, exceeds model limit of 100,000",
            "type": "invalid_request_error",
            "code": "context_overflow",
            "token_count": 150_001,
            "limit": 100_000,
        }
    }
    assert result.status_code == 413
    assert result.request is request
    assert result.json() == expected
    assert result.content == httpx.Response(413, json=expected).content
    assert result.headers["content-type"] == "application/json"
    assert request.content == original_body
    assert request.headers["authorization"] == "Bearer fixture-primary"
    assert build_response.call_args.args[0] is request
    assert build_response.call_args.args[1].request_size_bytes == len(original_body)
    transport.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("explicit", "environment", "expected"),
    [(None, "0", 73), (None, "80", 80), (0, "80", 80), (60, "80", 60)],
)
async def test_max_context_fallback_still_controls_send(
    mode, counter, monkeypatch, explicit, environment, expected
):
    monkeypatch.setattr(reasoning, "DEFAULT_MAX_CONTEXT_TOKENS", 73)
    monkeypatch.setenv("MAX_CONTEXT_TOKENS", environment)
    counter.return_value = expected + 1
    transport = Mock(return_value=chat_response())
    async with client_for(mode, transport, max_context_tokens=explicit) as client:
        result = await send(mode, client, request_for())
    assert result.status_code == 413
    assert result.json()["error"]["limit"] == expected
    transport.assert_not_called()


@pytest.mark.asyncio
async def test_warning_ratio_remains_a_module_global(
    mode, counter, monkeypatch, caplog
):
    counter.return_value = 51
    monkeypatch.setattr(reasoning, "WARNING_THRESHOLD_RATIO", 0.5)
    with caplog.at_level(logging.WARNING, logger=reasoning.__name__):
        async with client_for(
            mode, lambda request: chat_response(), max_context_tokens=100
        ) as client:
            assert (await send(mode, client, request_for())).status_code == 200
    assert "Request approaching context limit: 51/100 tokens (51.0%)" in caplog.text


@pytest.mark.asyncio
async def test_overflow_response_construction_is_outside_counting_allow_through(
    mode, counter, monkeypatch
):
    counter.return_value = 101
    build_response = Mock(side_effect=RuntimeError("fixture response construction"))
    monkeypatch.setattr(reasoning, "_overflow_response_413", build_response)
    transport = Mock(return_value=chat_response())
    async with client_for(mode, transport, max_context_tokens=100) as client:
        with pytest.raises(RuntimeError, match="fixture response construction"):
            await send(mode, client, request_for())
    build_response.assert_called_once()
    transport.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("api", ["chat", "responses"])
async def test_real_sdk_surfaces_413_without_retry_or_connection_wrapping(
    mode, counter, api
):
    counter.return_value = 101
    transport = Mock(return_value=chat_response())
    async with client_for(mode, transport, max_context_tokens=100) as client:
        sdk_type = openai.OpenAI if mode == "sync" else openai.AsyncOpenAI
        sdk = sdk_type(
            api_key="fixture-sdk-key",
            base_url="https://fixture.invalid/v1",
            http_client=client,
            max_retries=3,
        )
        # A regression may retry, but should not spend seconds in SDK backoff.
        sdk._calculate_retry_timeout = lambda *args, **kwargs: 0
        try:
            with pytest.raises(openai.APIStatusError) as caught:
                if api == "chat":
                    call = sdk.chat.completions.create(
                        model="fixture-model",
                        messages=[{"role": "user", "content": "fixture"}],
                    )
                else:
                    call = sdk.responses.create(model="fixture-model", input="fixture")
                if mode == "async":
                    await call
            assert caught.value.status_code == 413
            assert caught.value.code == "context_overflow"
            assert caught.value.body == {
                "message": "Request body has 101 tokens, exceeds model limit of 100",
                "type": "invalid_request_error",
                "code": "context_overflow",
                "token_count": 101,
                "limit": 100,
            }
            assert not isinstance(caught.value, openai.APIConnectionError)
        finally:
            if mode == "sync":
                sdk.close()
            else:
                await sdk.close()
    counter.assert_called_once()
    transport.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "body", "headers", "rotates"),
    [
        (401, {"error": "auth"}, {}, True),
        (403, {"error": "auth"}, {}, True),
        (429, {"error": "quota exceeded"}, {}, True),
        (429, {"error": "slow down"}, {"retry-after": "1"}, False),
    ],
)
async def test_rotation_keeps_its_own_single_retry_and_reasoning_capture(
    mode, counter, status, body, headers, rotates
):
    request = request_for()
    original_body = request.content
    first = httpx.Response(status, json=body, headers=headers)
    second = chat_response()
    seen = []

    def transport(sent):
        seen.append((sent, sent.headers["authorization"], sent.content))
        return first if len(seen) == 1 else second

    ring = KeyRing(["fixture-primary", "fixture-secondary"], cooldown_seconds=3600)
    async with client_for(mode, transport, key_ring=ring) as client:
        result = await send(mode, client, request)
        assert result is (second if rotates else first)
        assert client._last_reasoning_content == ("reason" if rotates else None)
    assert seen == [(request, "Bearer fixture-primary", original_body)] + (
        [(request, "Bearer fixture-secondary", original_body)] if rotates else []
    )
    counter.assert_called_once()


class TrackedSyncStream(httpx.SyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.events = []

    def __iter__(self):
        for chunk in self.chunks:
            self.events.append("read")
            yield chunk

    def close(self):
        self.events.append("close")


class TrackedAsyncStream(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.events = []

    async def __aiter__(self):
        for chunk in self.chunks:
            self.events.append("read")
            yield chunk

    async def aclose(self):
        self.events.append("close")


@pytest.mark.asyncio
async def test_stream_is_unread_until_consumer_iterates_and_closes(mode, counter):
    chunks = [
        b'data: {"choices":[{"delta":{"reasoning_content":"first "}}]}\n\n',
        b'data: {"choices":[{"delta":{"reasoning_content":"second"}}]}\n\n',
        b'data: {"choices":[{"delta":{"content":"answer"}}]}\n\ndata: [DONE]\n\n',
    ]
    stream_type = TrackedSyncStream if mode == "sync" else TrackedAsyncStream
    upstream = stream_type(chunks)
    response = httpx.Response(
        200, stream=upstream, headers={"content-type": "text/event-stream"}
    )
    async with client_for(mode, lambda request: response) as client:
        result = await send(mode, client, request_for(), stream=True)
        assert result is response
        assert upstream.events == []
        assert not result.is_closed
        assert not result.is_stream_consumed
        assert client._active_stream_tap is not None
        assert client._last_reasoning_content is None
        if mode == "sync":
            received = list(result.iter_bytes())
        else:
            received = [chunk async for chunk in result.aiter_bytes()]
        assert received == chunks
        assert upstream.events == ["read", "read", "read", "close"]
        assert result.is_closed
        assert client.consume_streamed_reasoning() == "first second"
        assert client.consume_streamed_reasoning() is None
    counter.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_responses_diagnostic_stays_async_and_nonstream_only(
    mode, monkeypatch, stream
):
    dump = Mock()
    monkeypatch.setattr(reasoning, "_dump_codex_raw_response", dump)
    monkeypatch.setenv("DEBUG_CODEX_RAW_RESPONSE", "true")
    request = request_for("/v1/responses")
    response = chat_response()
    async with client_for(mode, lambda request: response) as client:
        result = await send(mode, client, request, stream=stream)
        assert result is response
        assert client._active_stream_tap is None
        assert client._last_reasoning_content is None
        if mode == "async" and not stream:
            dump.assert_called_once_with(request, response)
        else:
            dump.assert_not_called()
