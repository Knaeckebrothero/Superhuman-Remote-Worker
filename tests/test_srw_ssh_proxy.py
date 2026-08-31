import importlib.util
import pathlib
from types import SimpleNamespace

import pytest

SPEC = importlib.util.spec_from_loader(
    "srw_ssh_proxy",
    importlib.machinery.SourceFileLoader(
        "srw_ssh_proxy",
        str(pathlib.Path(__file__).resolve().parents[1] / "scripts" / "srw-ssh-proxy"),
    ),
)
proxy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(proxy)


# ---------------------------------------------------------------------------
# Pinned contract tests (task-1-brief.md Step 1) — do not change these.
# ---------------------------------------------------------------------------


def test_handshake_requests_an_upgrade():
    request = proxy.build_handshake(
        "wss://api.srw.works/api/ssh/attach", "tok", "https://cockpit.srw.works"
    )
    text = request.decode()
    assert text.startswith("GET /api/ssh/attach HTTP/1.1\r\n")
    assert "Upgrade: websocket\r\n" in text
    assert "Connection: Upgrade\r\n" in text
    assert "Sec-WebSocket-Version: 13\r\n" in text
    assert "Host: api.srw.works\r\n" in text


def test_handshake_carries_the_token_and_origin():
    """The WSS endpoint authenticates before the upgrade and checks Origin."""
    request = proxy.build_handshake(
        "wss://api.srw.works/api/ssh/attach", "tok", "https://cockpit.srw.works"
    ).decode()
    assert "Authorization: Bearer tok\r\n" in request
    assert "Origin: https://cockpit.srw.works\r\n" in request


def test_client_frames_are_masked():
    """RFC 6455: a client MUST mask. An unmasked frame is a protocol error."""
    encoded = proxy.frame(b"hello", proxy.OPCODE_BINARY)
    assert encoded[1] & 0x80, "mask bit not set"


@pytest.mark.parametrize("size", [5, 200, 70000])
def test_frames_round_trip_at_every_length_class(size):
    payload = b"x" * size
    frames, rest = proxy.parse_frames(proxy.frame(payload, proxy.OPCODE_BINARY, mask=False))
    assert rest == b""
    assert frames == [(proxy.OPCODE_BINARY, payload)]


def test_partial_frame_is_buffered_not_dropped():
    """SSH is a byte stream; losing a partial frame corrupts the session."""
    encoded = proxy.frame(b"hello world", proxy.OPCODE_BINARY, mask=False)
    frames, rest = proxy.parse_frames(encoded[:4])
    assert frames == []
    assert rest == encoded[:4]
    frames, rest = proxy.parse_frames(encoded)
    assert frames == [(proxy.OPCODE_BINARY, b"hello world")]


def test_ping_frames_are_recognised():
    """Cloudflare closes idle WebSockets and documents a heartbeat as the fix."""
    frames, _ = proxy.parse_frames(proxy.frame(b"", proxy.OPCODE_PING, mask=False))
    assert frames[0][0] == proxy.OPCODE_PING


def test_close_frames_are_recognised():
    frames, _ = proxy.parse_frames(proxy.frame(b"", proxy.OPCODE_CLOSE, mask=False))
    assert frames[0][0] == proxy.OPCODE_CLOSE


# ---------------------------------------------------------------------------
# C1 — the attach token lives 300s; a durable PAT is exchanged for one on
# every connection. No sockets opened here: connection_cls is injected.
# ---------------------------------------------------------------------------


def _connection_returning(status, body):
    """A fake http.client.HTTPSConnection-alike that records what it was sent."""

    class _FakeConnection:
        instances: list["_FakeConnection"] = []

        def __init__(self, host, timeout=None):
            self.host = host
            self.timeout = timeout
            self.requested = None
            _FakeConnection.instances.append(self)

        def request(self, method, path, body=None, headers=None):
            self.requested = (method, path, body, headers)

        def getresponse(self):
            return SimpleNamespace(status=status, read=lambda: body)

        def close(self):
            pass

    return _FakeConnection


def _connection_raising(exc):
    class _FakeConnection:
        def __init__(self, host, timeout=None):
            pass

        def request(self, method, path, body=None, headers=None):
            raise exc

        def getresponse(self):  # pragma: no cover - never reached
            raise AssertionError("getresponse called after request() raised")

        def close(self):
            pass

    return _FakeConnection


def test_fetch_attach_token_parses_the_minted_token():
    body = b'{"token": "srw-sshws1:...", "expires_at": "2026-08-29T00:05:00+00:00"}'
    conn_cls = _connection_returning(200, body)
    token, expires_at = proxy.fetch_attach_token(
        "api.srw.works", "pat-value", connection_cls=conn_cls
    )
    assert token == "srw-sshws1:..."
    assert expires_at == "2026-08-29T00:05:00+00:00"
    method, path, sent_body, headers = conn_cls.instances[0].requested
    assert method == "POST"
    assert path == "/api/ssh/attach-token"
    assert headers["Authorization"] == "Bearer pat-value"


def test_fetch_attach_token_reports_unauthorized_distinctly():
    """401/403 (bad or unscoped PAT) must read differently from a connection error."""
    conn_cls = _connection_returning(401, b"unauthorized")
    with pytest.raises(SystemExit) as excinfo:
        proxy.fetch_attach_token("api.srw.works", "bad-pat", connection_cls=conn_cls)
    message = str(excinfo.value)
    assert "401" in message
    assert "PAT" in message


def test_fetch_attach_token_reports_connection_errors_distinctly():
    conn_cls = _connection_raising(OSError("Name or service not known"))
    with pytest.raises(SystemExit) as excinfo:
        proxy.fetch_attach_token("api.srw.works", "pat-value", connection_cls=conn_cls)
    message = str(excinfo.value)
    assert "401" not in message
    assert "Name or service not known" in message


def test_resolve_ws_token_prefers_the_override_and_skips_the_exchange(monkeypatch):
    """SRW_SSH_TOKEN is a one-off token pasted from the panel; no PAT needed."""
    monkeypatch.setenv("SRW_SSH_TOKEN", "one-off-token")

    def _fail_if_called(host, pat):
        raise AssertionError("fetch_attach_token must not run when the override is set")

    monkeypatch.setattr(proxy, "fetch_attach_token", _fail_if_called)
    assert proxy.resolve_ws_token("api.srw.works") == "one-off-token"


def test_resolve_ws_token_exchanges_the_pat_on_every_call(monkeypatch):
    monkeypatch.delenv("SRW_SSH_TOKEN", raising=False)
    monkeypatch.setenv("SRW_TOKEN", "durable-pat")
    calls = []

    def _stub(host, pat):
        calls.append((host, pat))
        return ("short-lived-token", "2026-08-29T00:05:00+00:00")

    monkeypatch.setattr(proxy, "fetch_attach_token", _stub)
    assert proxy.resolve_ws_token("api.srw.works") == "short-lived-token"
    assert calls == [("api.srw.works", "durable-pat")]
    # A second connection must mint a fresh token, not reuse the first.
    calls.clear()
    assert proxy.resolve_ws_token("api.srw.works") == "short-lived-token"
    assert len(calls) == 1


def test_resolve_ws_token_fails_closed_with_no_credential(monkeypatch):
    monkeypatch.delenv("SRW_SSH_TOKEN", raising=False)
    monkeypatch.setattr(proxy, "_load_pat", lambda: "")
    with pytest.raises(SystemExit):
        proxy.resolve_ws_token("api.srw.works")


# ---------------------------------------------------------------------------
# C3 — draining an SSLSocket. OpenSSL buffers a whole decrypted TLS record;
# bytes already sitting there never make a plain socket recv() readable
# again to select(), so a naive single recv() per select() wakeup can stall.
# ---------------------------------------------------------------------------


class _FakeSSLSocket:
    def __init__(self, chunks, pending_after):
        """``chunks`` is popped left-to-right by recv(); ``pending_after`` is
        the pending() value to report right after each recv() call, indexed
        the same way."""
        self._chunks = list(chunks)
        self._pending_after = list(pending_after)

    def recv(self, _n):
        return self._chunks.pop(0)

    def pending(self):
        return self._pending_after.pop(0)


def test_recv_available_drains_everything_openssl_already_decrypted():
    # First recv() returns a chunk and reports 1 more byte pending; the second
    # recv() drains it and reports nothing left.
    sock = _FakeSSLSocket(chunks=[b"hello ", b"world"], pending_after=[5, 0])
    assert proxy._recv_available(sock) == b"hello world"


def test_recv_available_returns_empty_on_a_closed_socket():
    sock = _FakeSSLSocket(chunks=[b""], pending_after=[])
    assert proxy._recv_available(sock) == b""


# ---------------------------------------------------------------------------
# C4 — the gateway fails closed on an Origin mismatch with no hint in the
# response; the helper must supply one instead of failing opaquely.
# ---------------------------------------------------------------------------


def test_handshake_error_hints_at_origin_on_403():
    message = proxy._handshake_error("HTTP/1.1 403 Forbidden", "https://cockpit.srw.works")
    assert "403" in message
    assert "cockpit.srw.works" in message
    assert "--origin" in message


def test_handshake_error_has_no_origin_hint_for_other_statuses():
    message = proxy._handshake_error("HTTP/1.1 500 Internal Server Error", "https://x")
    assert "--origin" not in message


def test_default_origin_guesses_the_cockpit_subdomain():
    assert proxy._default_origin("api.srw.works") == "https://cockpit.srw.works"
