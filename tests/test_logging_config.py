"""Tests for structured logging — orchestrator and agent share behaviour.

Both modules are independent copies (separate images, no shared import path),
so every test runs against both to catch drift, especially in ``redact()``.
"""

import json
import logging

import pytest

from orchestrator import logging_config as orch_log
from src.core import logging_config as agent_log


@pytest.fixture(params=[orch_log, agent_log], ids=["orchestrator", "agent"])
def mod(request):
    """The logging module under test (both flavours)."""
    return request.param


def _record(msg, *args, exc_info=None):
    return logging.LogRecord(
        name="test.logger",
        level=logging.INFO,
        pathname="/src/app/file.py",
        lineno=42,
        msg=msg,
        args=args,
        exc_info=exc_info,
    )


def _emit_json(mod, msg, *args, exc_info=None):
    return json.loads(
        mod.JsonLogFormatter(component="test").format(
            _record(msg, *args, exc_info=exc_info)
        )
    )


class TestJsonFormatter:
    def test_output_is_parseable_with_core_fields(self, mod):
        rec = _emit_json(mod, "hello %s", "world")
        assert rec["message"] == "hello world"
        assert rec["level"] == "INFO"
        assert rec["logger"] == "test.logger"
        assert rec["component"] == "test"
        assert rec["file"] == "file.py:42"
        assert rec["ts"].endswith("Z")

    def test_correlation_context_injected(self, mod):
        with mod.log_context(job_id="job-123", agent_id="agent-7"):
            rec = _emit_json(mod, "working")
        assert rec["job_id"] == "job-123"
        assert rec["agent_id"] == "agent-7"

    def test_context_cleared_after_scope(self, mod):
        with mod.log_context(job_id="job-123"):
            pass
        rec = _emit_json(mod, "after")
        assert "job_id" not in rec

    def test_bind_reset_roundtrip(self, mod):
        token = mod.bind_log_context(thread_id="t-1")
        assert _emit_json(mod, "x")["thread_id"] == "t-1"
        mod.reset_log_context(token)
        assert "thread_id" not in _emit_json(mod, "y")

    def test_none_and_empty_values_skipped(self, mod):
        # k8s sets unused env vars to "" (e.g. SESSION_BOUND_THREAD_ID on
        # worker pods); "" must not leak as a noise field.
        with mod.log_context(job_id=None, thread_id=""):
            rec = _emit_json(mod, "no ids")
        assert "job_id" not in rec
        assert "thread_id" not in rec

    def test_exc_info_included(self, mod):
        try:
            raise ValueError("boom")
        except ValueError:
            import sys

            rec = _emit_json(mod, "failed", exc_info=sys.exc_info())
        assert "ValueError: boom" in rec["exc"]


class TestRedaction:
    @pytest.mark.parametrize(
        "secret",
        [
            "api_key=sk-abcdef1234567890ABCDEFGH",
            "Authorization: Bearer eyJhbGciOi.JzdWIiOiI.SflKxwRJ",
            "password: hunter2supersecret",
            "client_secret=ZZxx9911aabbccddeeff",
            'token="ghp_ABCDEFGHIJKLMNOP1234567890"',
        ],
    )
    def test_secrets_are_masked(self, mod, secret):
        out = mod.redact(secret)
        assert mod._REDACTED in out
        # The raw secret value must not survive.
        for needle in ("sk-abcdef", "eyJhbGci", "hunter2", "ZZxx9911", "ghp_ABCDEF"):
            if needle in secret:
                assert needle not in out

    def test_non_secret_text_untouched(self, mod):
        msg = "job job-123 completed in 4200ms with status ok"
        assert mod.redact(msg) == msg

    def test_json_message_is_redacted(self, mod):
        rec = _emit_json(mod, "dispatching with api_key=sk-LIVE1234567890abcdefXYZ")
        assert "sk-LIVE1234567890abcdefXYZ" not in json.dumps(rec)
        assert mod._REDACTED in rec["message"]

    def test_redacted_json_stays_valid(self, mod):
        # redaction must not break JSON parsing (no stray quotes/newlines)
        line = mod.JsonLogFormatter(component="test").format(
            _record("Authorization: Bearer eyJabc.def.ghijklmnop")
        )
        json.loads(line)  # must not raise


class TestCorrelationIdMiddleware:
    """Orchestrator-only ASGI middleware — request_id for the whole request."""

    async def _request_id_seen_downstream(self, headers):
        seen = {}

        async def downstream(scope, receive, send):
            # Must be visible to the downstream app (i.e. route handlers).
            seen["request_id"] = orch_log._log_context.get().get("request_id")

        mw = orch_log.CorrelationIdMiddleware(downstream)

        async def receive():
            return {"type": "http.request"}

        async def send(_message):
            return None

        await mw({"type": "http", "headers": headers}, receive, send)
        return seen["request_id"]

    @pytest.mark.asyncio
    async def test_honors_inbound_request_id(self):
        rid = await self._request_id_seen_downstream(
            [(b"x-request-id", b"trace-abc-123")]
        )
        assert rid == "trace-abc-123"
        # bound only for the request — cleared afterwards
        assert "request_id" not in orch_log._log_context.get()

    @pytest.mark.asyncio
    async def test_generates_when_absent(self):
        rid = await self._request_id_seen_downstream([])
        assert rid and len(rid) == 12

    @pytest.mark.asyncio
    async def test_sanitizes_hostile_request_id(self):
        rid = await self._request_id_seen_downstream(
            [(b"x-request-id", b'bad\nid"<script>')]
        )
        assert rid is not None
        for bad in ("\n", '"', "<", ">"):
            assert bad not in rid

    @pytest.mark.asyncio
    async def test_non_http_scope_passes_through(self):
        called = {}

        async def downstream(scope, receive, send):
            called["ok"] = True

        mw = orch_log.CorrelationIdMiddleware(downstream)
        await mw({"type": "websocket"}, None, None)
        assert called["ok"]
