import http.client
import importlib.util
import os
import pathlib
import socket
import ssl
import threading
import time
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
    frames, rest = proxy.parse_frames(
        proxy.frame(payload, proxy.OPCODE_BINARY, mask=False)
    )
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
# I-7 / masking correctness — every pinned round-trip test above passes
# mask=False, so the unmask branch is otherwise dead in the suite.
# ---------------------------------------------------------------------------


def test_masked_frames_unmask_correctly_on_round_trip():
    """An implementation that sets the mask bit but ships the payload in the
    clear would still pass test_client_frames_are_masked alone."""
    payload = b"the quick brown fox jumps over the lazy dog" * 50
    frames, rest = proxy.parse_frames(
        proxy.frame(payload, proxy.OPCODE_BINARY, mask=True)
    )
    assert rest == b""
    assert frames == [(proxy.OPCODE_BINARY, payload)]


# ---------------------------------------------------------------------------
# Minor — no maximum frame length / MSB-set 64-bit length.
# ---------------------------------------------------------------------------


def test_parse_frames_rejects_a_length_with_the_msb_set():
    # RFC 6455: the 64-bit length's most significant bit MUST be 0.
    header = bytes([0x82, 127]) + (1 << 63).to_bytes(8, "big")
    with pytest.raises(ValueError):
        proxy.parse_frames(header)


def test_parse_frames_rejects_a_payload_over_the_cap():
    too_big = proxy.MAX_FRAME_PAYLOAD + 1
    header = bytes([0x82, 127]) + too_big.to_bytes(8, "big")
    with pytest.raises(ValueError):
        proxy.parse_frames(header)


def test_max_frame_payload_is_reconciled_with_the_queue_cap():
    """N-4 (findings-r2.md): a 16 MiB frame cap against a 4 MiB queue cap
    was a 256x gap -- one frame could add 4x the entire queue budget in a
    single _consume_frames call. Pin them to the same order of magnitude
    rather than any specific pair of numbers."""
    assert proxy.MAX_FRAME_PAYLOAD <= proxy.MAX_QUEUE_BYTES


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


def test_fetch_attach_token_reports_tls_errors_distinctly():
    """Minor: ssl.SSLError is an OSError subclass; it must not be reported as
    'could not reach' -- a cert failure and a DNS failure need different
    fixes."""
    conn_cls = _connection_raising(ssl.SSLError("certificate verify failed"))
    with pytest.raises(SystemExit) as excinfo:
        proxy.fetch_attach_token("api.srw.works", "pat-value", connection_cls=conn_cls)
    message = str(excinfo.value)
    assert "TLS" in message
    assert "could not reach" not in message


def test_fetch_attach_token_reports_malformed_http_responses_distinctly():
    """Minor: http.client.HTTPException is not an OSError subclass and was
    previously an uncaught traceback instead of a srw-ssh-proxy: line."""
    conn_cls = _connection_raising(http.client.BadStatusLine("garbage"))
    with pytest.raises(SystemExit) as excinfo:
        proxy.fetch_attach_token("api.srw.works", "pat-value", connection_cls=conn_cls)
    message = str(excinfo.value)
    assert "malformed" in message.lower()
    assert "could not reach" not in message


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
# Minor — the PAT file's permissions are never checked.
# ---------------------------------------------------------------------------


def _patch_config_token_path(monkeypatch, token_path):
    real_expanduser = os.path.expanduser

    def _fake_expanduser(path):
        if path == "~/.config/srw/token":
            return str(token_path)
        return real_expanduser(path)

    monkeypatch.setattr(proxy.os.path, "expanduser", _fake_expanduser)


def test_load_pat_warns_when_the_token_file_is_group_or_world_readable(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.delenv("SRW_TOKEN", raising=False)
    token_path = tmp_path / "token"
    token_path.write_text("pat-value\n")
    token_path.chmod(0o644)
    _patch_config_token_path(monkeypatch, token_path)
    assert proxy._load_pat() == "pat-value"
    assert "group or other" in capsys.readouterr().err


def test_load_pat_is_quiet_when_the_token_file_is_private(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.delenv("SRW_TOKEN", raising=False)
    token_path = tmp_path / "token"
    token_path.write_text("pat-value\n")
    token_path.chmod(0o600)
    _patch_config_token_path(monkeypatch, token_path)
    assert proxy._load_pat() == "pat-value"
    assert capsys.readouterr().err == ""


# ---------------------------------------------------------------------------
# C3 — draining an SSLSocket. OpenSSL buffers a whole decrypted TLS record;
# bytes already sitting there never make a plain socket recv() readable
# again to select(), so a naive single recv() per select() wakeup can stall.
# ---------------------------------------------------------------------------


class _FakeSSLSocket:
    def __init__(self, chunks, pending_after):
        """``chunks`` is popped left-to-right by recv(); an item that is an
        exception *instance* is raised instead of returned, so the same fake
        can simulate a would-block on any recv() call. ``pending_after`` is
        the pending() value to report right after each successful recv()
        call, indexed the same way."""
        self._chunks = list(chunks)
        self._pending_after = list(pending_after)

    def recv(self, _n):
        item = self._chunks.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def pending(self):
        return self._pending_after.pop(0)


def test_recv_available_drains_everything_openssl_already_decrypted():
    # First recv() returns a chunk and reports 1 more byte pending; the second
    # recv() drains it and reports nothing left.
    sock = _FakeSSLSocket(chunks=[b"hello ", b"world"], pending_after=[5, 0])
    assert proxy._recv_available(sock) == b"hello world"


def test_recv_available_returns_none_not_eof_on_a_would_block_first_recv():
    """N-1 (findings-r2.md): reproduced live by the reviewer -- a 16 KiB TLS
    record split across TCP segments (any real ~1500-byte-MTU path, i.e.
    Task 7's scp) or a post-handshake NewSessionTicket/KeyUpdate both raise
    SSLWantReadError on the very first recv(), on a socket this design made
    non-blocking. That must return something _pump can tell apart from a
    real close, not collapse into the same b"" (which is the trap: wrapping
    just the first recv() in `except _WOULD_BLOCK: return b""` would pass
    the test below on its own but kill a healthy session in _pump)."""
    sock = _FakeSSLSocket(chunks=[ssl.SSLWantReadError()], pending_after=[])
    assert proxy._recv_available(sock) is None


def test_recv_available_still_returns_empty_bytes_not_none_on_real_eof():
    """The companion to the test above. One test cannot cover both cases
    (findings-r2.md's own warning): this pins that a real close still
    reports as b"", distinct from the None a would-block reports above."""
    sock = _FakeSSLSocket(chunks=[b""], pending_after=[])
    result = proxy._recv_available(sock)
    assert result == b""
    assert result is not None


# ---------------------------------------------------------------------------
# N-2 — os.read() has the identical would-block-vs-EOF trap as N-1, on the
# local fd. Real os.pipe() fds, not mocks: a would-block on an actually-empty
# non-blocking pipe is a genuine, deterministic OS behavior to test against.
# ---------------------------------------------------------------------------


def test_read_available_returns_none_when_nothing_is_ready():
    read_fd, write_fd = os.pipe()
    try:
        os.set_blocking(read_fd, False)
        assert proxy._read_available(read_fd) is None
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_read_available_returns_bytes_when_data_is_ready():
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, b"hello")
        os.set_blocking(read_fd, False)
        assert proxy._read_available(read_fd) == b"hello"
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_read_available_returns_empty_bytes_on_real_eof():
    read_fd, write_fd = os.pipe()
    os.close(write_fd)  # writer gone -> the reader sees a real EOF
    try:
        os.set_blocking(read_fd, False)
        result = proxy._read_available(read_fd)
        assert result == b""
        assert result is not None
    finally:
        os.close(read_fd)


# ---------------------------------------------------------------------------
# N-1/N-2 — _pump's read branches use one shared classifier for both
# _recv_available and _read_available results; pinned directly since it's
# the exact would-block-vs-EOF decision the regression was about.
# ---------------------------------------------------------------------------


def test_classify_chunk_would_block_means_continue():
    assert proxy._classify_chunk(None) == "continue"


def test_classify_chunk_real_eof_means_close():
    assert proxy._classify_chunk(b"") == "close"


def test_classify_chunk_nonempty_bytes_means_data():
    assert proxy._classify_chunk(b"hello") == "data"


# ---------------------------------------------------------------------------
# C4 — the gateway fails closed on an Origin mismatch with no hint in the
# response; the helper must supply one instead of failing opaquely. Also
# I-3: a bare 403 covers three separate pre-accept refusals, not just Origin.
# ---------------------------------------------------------------------------


def test_handshake_error_names_every_pre_accept_403_cause():
    message = proxy._handshake_error(
        "HTTP/1.1 403 Forbidden", "https://cockpit.srw.works"
    )
    assert "403" in message
    assert "cockpit.srw.works" in message
    assert "--origin" in message
    assert "token" in message.lower()
    assert "rate" in message.lower()


def test_handshake_error_has_no_origin_hint_for_other_statuses():
    message = proxy._handshake_error("HTTP/1.1 500 Internal Server Error", "https://x")
    assert "--origin" not in message


def test_default_origin_guesses_the_cockpit_subdomain():
    assert proxy._default_origin("api.srw.works") == "https://cockpit.srw.works"


# ---------------------------------------------------------------------------
# Minor — Sec-WebSocket-Accept is generated and never verified.
# ---------------------------------------------------------------------------


def test_compute_accept_matches_the_rfc6455_worked_example():
    # RFC 6455 section 1.3's worked example.
    assert (
        proxy._compute_accept("dGhlIHNhbXBsZSBub25jZQ==")
        == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="
    )


def test_extract_header_is_case_insensitive():
    blob = (
        b"HTTP/1.1 101 Switching Protocols\r\n"
        b"Sec-WebSocket-Accept: abc123\r\n"
        b"Upgrade: websocket"
    )
    assert proxy._extract_header(blob, "sec-websocket-accept") == "abc123"
    assert proxy._extract_header(blob, "SEC-WEBSOCKET-ACCEPT") == "abc123"
    assert proxy._extract_header(blob, "missing-header") == ""


# ---------------------------------------------------------------------------
# C-1 regression: bytes that arrive in the same TCP segment as the
# handshake's trailing blank line must reach the relay, not be dropped.
# _connect and _pump had no coverage at all, which is why this was invisible.
# ---------------------------------------------------------------------------


def test_split_handshake_response_returns_post_header_bytes_as_leftover():
    response = (
        b"HTTP/1.1 101 Switching Protocols\r\n"
        b"Upgrade: websocket\r\n"
        b"Connection: Upgrade\r\n"
        b"Sec-WebSocket-Accept: abc\r\n"
        b"\r\n"
        b"the-ssh-banner-that-coalesced-with-the-101"
    )
    status, header, leftover = proxy._split_handshake_response(response)
    assert status == "HTTP/1.1 101 Switching Protocols"
    assert leftover == b"the-ssh-banner-that-coalesced-with-the-101"
    assert b"Sec-WebSocket-Accept: abc" in header


def test_consume_frames_routes_a_seeded_leftover_buffer_to_the_relay():
    """The exact bug: _connect used to return only the socket, and _pump
    started buffer = b"", so a leftover like the one above was thrown away."""
    seeded = proxy.frame(b"SSH-2.0-banner", proxy.OPCODE_BINARY, mask=False)
    to_local = bytearray()
    to_remote = bytearray()
    remainder, should_stop = proxy._consume_frames(seeded, to_local, to_remote)
    assert bytes(to_local) == b"SSH-2.0-banner"
    assert should_stop is False
    assert remainder == b""


class _FakeRawConnectSocket:
    """Stands in for the TCP socket _connect creates via
    socket.create_connection, and -- paired with _FakeSSLContext below,
    which skips the real TLS handshake -- for the SSLSocket it wraps.

    Builds its canned handshake response lazily, on the first recv(), once
    ``self.sent`` already holds the complete request _connect just sent:
    that lets it compute a Sec-WebSocket-Accept that actually matches the
    random Sec-WebSocket-Key _connect generated, so the round-1
    Sec-WebSocket-Accept verification is genuinely exercised too, not
    bypassed.
    """

    def __init__(self, banner: bytes):
        self.sent = b""
        self._banner = banner
        self._response = None

    def sendall(self, data):
        self.sent += data

    def recv(self, n):
        if self._response is None:
            sent_key = proxy._extract_header(self.sent, "Sec-WebSocket-Key")
            accept = proxy._compute_accept(sent_key)
            self._response = (
                b"HTTP/1.1 101 Switching Protocols\r\n"
                b"Upgrade: websocket\r\n"
                b"Connection: Upgrade\r\n"
                b"Sec-WebSocket-Accept: " + accept.encode() + b"\r\n"
                b"\r\n" + self._banner
            )
        chunk, self._response = self._response[:n], self._response[n:]
        return chunk

    def settimeout(self, _value):
        pass

    def setblocking(self, _flag):
        pass

    def close(self):
        pass


class _FakeSSLContext:
    def wrap_socket(self, raw, server_hostname=None):
        return raw  # no real TLS: the fake raw socket already speaks plaintext


def test_connect_relays_leftover_bytes_from_a_real_coalesced_response(monkeypatch):
    """Residual C-1 gap (findings-r2.md): mutating _connect's own
    `return sock, leftover` to `return sock, b""` still passed 42/42,
    because _split_handshake_response is tested as a pure function and
    _pump is tested with an explicitly-passed buffer -- nothing pinned the
    wire between them, i.e. that _connect's own recv() loop actually
    produces and returns the real leftover. This does, with a fake
    create_connection/create_default_context -- no real network or TLS."""
    fake_raw = _FakeRawConnectSocket(banner=b"SSH-2.0-coalesced-banner")
    monkeypatch.setattr(proxy.socket, "create_connection", lambda *a, **k: fake_raw)
    monkeypatch.setattr(proxy.ssl, "create_default_context", lambda: _FakeSSLContext())
    monkeypatch.setattr(proxy, "resolve_ws_token", lambda host: "test-token")

    sock, leftover = proxy._connect("api.srw.works", "https://cockpit.srw.works")
    assert sock is fake_raw
    assert leftover == b"SSH-2.0-coalesced-banner"


class _FakeRawSocketThatClosesCleanly:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _CertFailingSSLContext:
    def __init__(self, raw_to_track):
        self._raw = raw_to_track

    def wrap_socket(self, raw, server_hostname=None):
        assert raw is self._raw
        raise ssl.SSLCertVerificationError("certificate verify failed: self-signed")


def test_connect_reports_a_bad_certificate_cleanly_and_closes_the_raw_socket(
    monkeypatch,
):
    """Minor (findings-r2.md): wrap_socket used to sit outside _connect's
    try block, so a bad certificate reached the user as a raw traceback --
    same class of problem as fetch_attach_token's TLS handling, a different
    site -- and the raw TCP socket it wraps was never closed either."""
    fake_raw = _FakeRawSocketThatClosesCleanly()
    monkeypatch.setattr(proxy.socket, "create_connection", lambda *a, **k: fake_raw)
    monkeypatch.setattr(
        proxy.ssl, "create_default_context", lambda: _CertFailingSSLContext(fake_raw)
    )
    monkeypatch.setattr(proxy, "resolve_ws_token", lambda host: "test-token")

    with pytest.raises(SystemExit) as excinfo:
        proxy._connect("api.srw.works", "https://cockpit.srw.works")
    assert "TLS error" in str(excinfo.value)
    assert fake_raw.closed is True


class _PendinglessSocket:
    """Wraps a real (local, no network) socket to satisfy _pump's minimal
    SSLSocket-shaped interface -- recv/send/setblocking/fileno come from the
    real socket; pending() always reports 0 since a plain socket has no
    OpenSSL-side buffer to drain."""

    def __init__(self, sock):
        self._sock = sock

    def fileno(self):
        return self._sock.fileno()

    def recv(self, n):
        return self._sock.recv(n)

    def send(self, data):
        return self._sock.send(data)

    def setblocking(self, flag):
        self._sock.setblocking(flag)

    def pending(self):
        return 0


def test_pump_relays_the_seeded_leftover_and_both_live_directions():
    """End-to-end coverage of _pump itself (not just _consume_frames), over
    real local socketpairs -- no network, no TLS, so this is not the
    "do not open sockets" case the token-exchange tests avoid; it is the
    select()/queue wiring around them that the review flagged as
    completely uncovered."""
    remote_a, remote_b = socket.socketpair()
    local_a, local_b = socket.socketpair()
    remote = _PendinglessSocket(remote_a)
    seeded = proxy.frame(b"SSH-2.0-banner", proxy.OPCODE_BINARY, mask=False)
    result = {}

    def _run():
        try:
            proxy._pump(
                remote, local_a.fileno(), local_a.fileno(), initial_buffer=seeded
            )
        except OSError as exc:  # pragma: no cover - surfaced via result below
            result["error"] = exc

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    try:
        local_b.settimeout(2.0)
        remote_b.settimeout(2.0)

        # C-1: the seeded leftover (the coalesced-with-101 banner) must reach
        # the local side, unprompted by anything the test sends.
        assert local_b.recv(65536) == b"SSH-2.0-banner"

        # Remote -> local over a live select() wakeup, not just the seed.
        remote_b.sendall(
            proxy.frame(b"more-remote-data", proxy.OPCODE_BINARY, mask=False)
        )
        assert local_b.recv(65536) == b"more-remote-data"

        # Local -> remote. Filtering for the BINARY frame (rather than
        # asserting it's the only or first frame) is a leftover defensive
        # habit from when last_ping started at 0.0 against
        # time.monotonic()'s arbitrary epoch, queuing a spurious PING on the
        # very first loop iteration of every connection -- fixed by seeding
        # last_ping = time.monotonic() instead; see
        # test_pump_does_not_ping_immediately_on_connect below, which pins
        # that fix directly.
        local_b.sendall(b"local-input")
        buf = b""
        binary_frames = []
        while not binary_frames:
            buf += remote_b.recv(65536)
            new_frames, buf = proxy.parse_frames(buf)
            binary_frames = [f for f in new_frames if f[0] == proxy.OPCODE_BINARY]
        assert binary_frames == [(proxy.OPCODE_BINARY, b"local-input")]

        # Local EOF -> a CLOSE reaches the remote and the pump exits.
        local_b.close()
        buf = b""
        seen_close = False
        while not seen_close:
            chunk = remote_b.recv(65536)
            if not chunk:
                break
            buf += chunk
            new_frames, buf = proxy.parse_frames(buf)
            seen_close = any(opcode == proxy.OPCODE_CLOSE for opcode, _ in new_frames)
        assert seen_close
    finally:
        thread.join(timeout=2.0)
        still_running = thread.is_alive()
        for sock in (remote_a, remote_b, local_a):
            try:
                sock.close()
            except OSError:
                pass
    assert not still_running, "pump did not exit after local EOF"
    assert "error" not in result


def test_pump_does_not_ping_immediately_on_connect():
    """Minor (findings-r2.md): last_ping used to start at 0.0 against
    time.monotonic()'s arbitrary epoch (often system uptime, not 0), so
    `now - last_ping > PING_INTERVAL_SECONDS` was true on the very first
    loop iteration of every connection, queuing a PING nobody asked for
    before any real traffic. Fixed by seeding last_ping = time.monotonic()
    at pump start; this asserts the very first frame the remote sees is the
    seeded banner itself, with no PING ahead of it."""
    remote_a, remote_b = socket.socketpair()
    local_a, local_b = socket.socketpair()
    remote = _PendinglessSocket(remote_a)
    seeded = proxy.frame(b"SSH-2.0-banner", proxy.OPCODE_BINARY, mask=False)

    thread = threading.Thread(
        target=proxy._pump,
        args=(remote, local_a.fileno(), local_a.fileno()),
        kwargs={"initial_buffer": seeded},
        daemon=True,
    )
    thread.start()
    try:
        local_b.settimeout(2.0)
        assert local_b.recv(65536) == b"SSH-2.0-banner"
        # local-input should still be un-preceded by anything else queued.
        local_b.sendall(b"probe")
        remote_b.settimeout(2.0)
        frames, _ = proxy.parse_frames(remote_b.recv(65536))
        assert frames == [(proxy.OPCODE_BINARY, b"probe")]
    finally:
        local_b.close()
        thread.join(timeout=2.0)
        for sock in (remote_a, remote_b, local_a):
            try:
                sock.close()
            except OSError:
                pass


class _OnceWouldBlockSocket:
    """Wraps a real socket; the ``raise_on_call_index``-th call to recv()
    raises ``exc`` instead of delegating -- letting a test force one
    genuine would-block through _pump's real select() loop (the fd is
    actually readable; the *application* read is what would-blocks), then
    resume normally. Models the reviewer's real-TLS repro (a record split
    across TCP segments) without needing actual TLS handshake machinery."""

    def __init__(self, sock, raise_on_call_index, exc):
        self._sock = sock
        self._raise_on = raise_on_call_index
        self._exc = exc
        self._calls = 0

    def fileno(self):
        return self._sock.fileno()

    def recv(self, n):
        call, self._calls = self._calls, self._calls + 1
        if call == self._raise_on:
            raise self._exc
        return self._sock.recv(n)

    def send(self, data):
        return self._sock.send(data)

    def setblocking(self, flag):
        self._sock.setblocking(flag)

    def pending(self):
        return 0


def test_pump_survives_a_spurious_remote_wakeup(monkeypatch):
    """N-1's actual regression site, not just _recv_available in isolation:
    mutation-tested (revert _pump's read branch to the old `if not chunk:
    return`, ignoring _classify_chunk's continue/close distinction) --
    every other test in this suite still passed. This forces one real
    SSLWantReadError out of recv() on data that has genuinely arrived (a
    real select() reports the fd readable), through the real _pump loop,
    and confirms the session survives and the data still arrives once the
    would-block clears -- rather than _pump treating it as a close."""
    remote_a, remote_b = socket.socketpair()
    local_a, local_b = socket.socketpair()
    remote = _OnceWouldBlockSocket(
        remote_a, raise_on_call_index=0, exc=ssl.SSLWantReadError()
    )
    result = {}

    def _run():
        try:
            proxy._pump(remote, local_a.fileno(), local_a.fileno())
        except OSError as exc:  # pragma: no cover - surfaced via result below
            result["error"] = exc

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    try:
        remote_b.sendall(proxy.frame(b"payload", proxy.OPCODE_BINARY, mask=False))
        local_b.settimeout(2.0)
        assert local_b.recv(65536) == b"payload"
    finally:
        local_b.close()
        thread.join(timeout=2.0)
        for sock in (remote_a, remote_b, local_a):
            try:
                sock.close()
            except OSError:
                pass
    assert "error" not in result


def test_pump_survives_a_spurious_local_read_wakeup(monkeypatch):
    """N-2's actual regression site, not just _read_available in isolation.

    Patches os.read itself (which _read_available wraps) rather than
    _read_available directly: a first attempt at patching _read_available
    instead did NOT catch the obvious revert-_pump's-branch-to-raw-os.read
    mutation, because that reverted code bypasses _read_available entirely
    -- the fake at that level simply never gets called under the mutation.
    Patching the lower layer forces a real BlockingIOError out of whichever
    of the two ends up calling os.read, so it catches both. This forces one
    real BlockingIOError -- exactly what _read_available's own dedicated
    tests above prove it turns into None -- through the real _pump loop,
    and confirms the session survives rather than _pump fabricating a local
    EOF."""
    remote_a, remote_b = socket.socketpair()
    local_a, local_b = socket.socketpair()
    remote = _PendinglessSocket(remote_a)
    real_os_read = proxy.os.read
    calls = {"n": 0}

    def _flaky_os_read(fd, n):
        if fd == local_a.fileno() and calls["n"] == 0:
            calls["n"] += 1
            raise BlockingIOError()
        return real_os_read(fd, n)

    monkeypatch.setattr(proxy.os, "read", _flaky_os_read)
    result = {}

    def _run():
        try:
            proxy._pump(remote, local_a.fileno(), local_a.fileno())
        except OSError as exc:  # pragma: no cover - surfaced via result below
            result["error"] = exc

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    try:
        local_b.sendall(b"local-payload")
        remote_b.settimeout(2.0)
        buf = b""
        binary_frames = []
        while not binary_frames:
            buf += remote_b.recv(65536)
            new_frames, buf = proxy.parse_frames(buf)
            binary_frames = [f for f in new_frames if f[0] == proxy.OPCODE_BINARY]
        assert binary_frames == [(proxy.OPCODE_BINARY, b"local-payload")]
    finally:
        local_b.close()
        thread.join(timeout=2.0)
        for sock in (remote_a, remote_b, local_a):
            try:
                sock.close()
            except OSError:
                pass
    assert "error" not in result


# ---------------------------------------------------------------------------
# I-2 — continuation frames were silently dropped.
# ---------------------------------------------------------------------------


def _raw_frame(fin: bool, opcode: int, payload: bytes) -> bytes:
    """One unmasked frame with an explicit FIN bit -- proxy.frame() always
    sets FIN=1, so a fragmented-message test needs this instead."""
    first_byte = (0x80 if fin else 0x00) | opcode
    length = len(payload)
    if length < 126:
        header = bytes([first_byte, length])
    elif length < 65536:
        header = bytes([first_byte, 126]) + length.to_bytes(2, "big")
    else:
        header = bytes([first_byte, 127]) + length.to_bytes(8, "big")
    return header + payload


def test_fragmented_message_continuation_payloads_are_relayed():
    first = _raw_frame(fin=False, opcode=proxy.OPCODE_BINARY, payload=b"hello ")
    second = _raw_frame(fin=True, opcode=proxy.OPCODE_CONTINUATION, payload=b"world")
    to_local = bytearray()
    to_remote = bytearray()
    remainder, should_stop = proxy._consume_frames(first + second, to_local, to_remote)
    assert bytes(to_local) == b"hello world"
    assert should_stop is False
    assert remainder == b""


def test_interleaved_control_frames_are_handled_within_one_buffer():
    buffer = (
        proxy.frame(b"AAA", proxy.OPCODE_BINARY, mask=False)
        + proxy.frame(b"ping-body", proxy.OPCODE_PING, mask=False)
        + proxy.frame(b"BBB", proxy.OPCODE_BINARY, mask=False)
        + proxy.frame(b"", proxy.OPCODE_CLOSE, mask=False)
        + proxy.frame(
            b"CCC", proxy.OPCODE_BINARY, mask=False
        )  # after CLOSE: not processed
    )
    to_local = bytearray()
    to_remote = bytearray()
    remainder, should_stop = proxy._consume_frames(buffer, to_local, to_remote)
    assert bytes(to_local) == b"AAABBB"
    assert should_stop is True
    frames_out, _ = proxy.parse_frames(bytes(to_remote))
    opcodes = [op for op, _ in frames_out]
    assert proxy.OPCODE_PONG in opcodes
    assert proxy.OPCODE_CLOSE in opcodes
    pong_payload = next(
        payload for op, payload in frames_out if op == proxy.OPCODE_PONG
    )
    assert pong_payload == b"ping-body"


# ---------------------------------------------------------------------------
# I-5 — blocking sendall inside the select loop could deadlock the relay.
# _flush_queue is the non-blocking-send-with-a-queue fix; this is the part
# that has to be exactly right, since a wedge here fails Task 7's ~1 GB scp
# and JetBrains's multi-GB backend pull.
# ---------------------------------------------------------------------------


def test_flush_queue_does_nothing_on_an_empty_queue():
    calls = []
    proxy._flush_queue(lambda data: calls.append(data) or len(data), bytearray())
    assert calls == []


def test_flush_queue_drops_only_the_bytes_actually_sent_on_a_partial_write():
    queue = bytearray(b"0123456789")
    proxy._flush_queue(lambda data: 4, queue)  # pretend only 4 bytes went out
    assert queue == bytearray(b"456789")


def test_flush_queue_drains_fully_when_the_sender_accepts_everything():
    queue = bytearray(b"hello")

    def _sender(data):
        return len(data)

    proxy._flush_queue(_sender, queue)
    assert queue == bytearray(b"")


def test_flush_queue_leaves_the_queue_untouched_on_blockingioerror():
    """The OS write buffer being full is normal backpressure, not a failure --
    the remainder must stay queued for the next writable wakeup, not be
    dropped or raised past the caller."""
    queue = bytearray(b"payload")

    def _sender(_data):
        raise BlockingIOError()

    proxy._flush_queue(_sender, queue)
    assert queue == bytearray(b"payload")


def test_flush_queue_treats_ssl_want_write_as_backpressure_too():
    """sock.send() on a non-blocking SSLSocket can raise SSLWantWriteError
    instead of BlockingIOError; both mean the same thing here."""
    queue = bytearray(b"payload")

    def _sender(_data):
        raise ssl.SSLWantWriteError()

    proxy._flush_queue(_sender, queue)
    assert queue == bytearray(b"payload")


def test_flush_queue_passes_a_memoryview_not_a_full_copy():
    """N-5 (findings-r2.md): sender(bytes(queue)) copied up to
    MAX_QUEUE_BYTES on every writable wakeup per direction -- a real cost on
    the critical path of the 1 GB transfer this design exists to survive.
    memoryview(queue) is O(1)."""
    queue = bytearray(b"payload")
    received_types = []

    def _sender(data):
        received_types.append(type(data))
        return len(data)

    proxy._flush_queue(_sender, queue)
    assert received_types == [memoryview]


# ---------------------------------------------------------------------------
# N-3/N-6 — _drain_final's single re-blocked os.write/sendall could silently
# truncate a short write and could hang forever on a wedged consumer, which
# would leak the pump's thread and both fds permanently in --listen mode.
# It also only restored blocking mode inside `if to_local:`, leaving
# stdin/stdout non-blocking on the common (empty-queue) clean-exit path.
# ---------------------------------------------------------------------------


def test_drain_write_with_deadline_loops_over_short_writes():
    """os.write is not write_all -- POSIX permits a short count."""
    sent_chunks = []

    def _sender(data):
        chunk = bytes(data)[:3]  # simulate a short write, 3 bytes at a time
        sent_chunks.append(chunk)
        return len(chunk)

    proxy._drain_write_with_deadline(
        object(), _sender, b"0123456789", time.monotonic() + 5.0
    )
    assert b"".join(sent_chunks) == b"0123456789"


def test_drain_write_with_deadline_gives_up_on_an_already_expired_deadline():
    attempts = []

    def _sender(_data):
        attempts.append(1)
        return 1

    started = time.monotonic()
    proxy._drain_write_with_deadline(object(), _sender, b"payload", started - 1.0)
    elapsed = time.monotonic() - started
    assert elapsed < 1.0
    assert attempts == []  # the deadline check comes before the first send


def test_drain_write_with_deadline_bounds_a_genuinely_wedged_consumer():
    """A consumer that never drains anything must not hang this forever --
    that would leak _pump's thread and both fds permanently in --listen
    mode (the same class of leak ruling I-4 was raised for). Uses a real
    socketpair with its send buffer filled to genuinely force
    BlockingIOError, so the would-block/retry-via-select path is actually
    exercised, not just the already-expired-deadline fast path above."""
    a, b = socket.socketpair()
    try:
        a.setblocking(False)
        try:
            while True:
                a.send(b"x" * 65536)
        except BlockingIOError:
            pass  # a's send buffer is now full; b never reads it

        started = time.monotonic()
        proxy._drain_write_with_deadline(a, a.send, b"tail", started + 0.3)
        elapsed = time.monotonic() - started
        assert elapsed < 2.0  # bounded, not hung forever
    finally:
        a.close()
        b.close()


def test_drain_write_with_deadline_stops_cleanly_on_oserror():
    def _sender(_data):
        raise BrokenPipeError()

    # Must not raise past the caller -- this runs from _pump's finally.
    proxy._drain_write_with_deadline(
        object(), _sender, b"payload", time.monotonic() + 5.0
    )


def test_drain_final_restores_blocking_mode_even_with_empty_queues(monkeypatch):
    """N-6: the previous version's os.set_blocking(write_fd, True) sat
    inside `if to_local:`, so the common case -- a clean shutdown with
    nothing left queued -- left the fd non-blocking on exit. That's the
    scenario this test pins: both queues empty."""
    read_fd, write_fd = os.pipe()
    try:
        os.set_blocking(write_fd, False)

        class _FakeSock:
            def setblocking(self, flag):
                self.blocking = flag

        fake_sock = _FakeSock()
        proxy._drain_final(write_fd, fake_sock, bytearray(), bytearray())

        # os.get_blocking is the direct way to observe the fd's own flag.
        assert os.get_blocking(write_fd) is True
        assert fake_sock.blocking is True
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_drain_final_flushes_a_queued_close_echo_to_completion():
    """End-to-end sanity check with real pipes/sockets: a queued remainder
    (the shape _drain_final exists to flush -- typically a CLOSE echo) is
    fully delivered through _drain_final's actual wiring of write_fd/sock
    into _drain_write_with_deadline, not just at that helper's own level.
    Kept comfortably under a pipe's default ~64 KiB kernel buffer so this
    doesn't need a concurrent reader draining it -- the retry-until-fully-
    sent behavior itself is already pinned directly at the
    _drain_write_with_deadline level above with a fake sender."""
    read_fd, write_fd = os.pipe()
    remote_a, remote_b = socket.socketpair()
    try:
        os.set_blocking(write_fd, False)
        to_local = bytearray(b"x" * 1000)
        to_remote = bytearray(b"y" * 1000)
        proxy._drain_final(write_fd, remote_a, to_local, to_remote)

        os.set_blocking(read_fd, False)
        assert os.read(read_fd, 4000) == b"x" * 1000

        remote_b.settimeout(2.0)
        assert remote_b.recv(4000) == b"y" * 1000
    finally:
        os.close(read_fd)
        os.close(write_fd)
        remote_a.close()
        remote_b.close()


# ---------------------------------------------------------------------------
# I-1 — every failure in --listen mode was completely silent (SystemExit
# derives from BaseException; threading.excepthook drops it silently).
# ---------------------------------------------------------------------------


class _FakeClient:
    def __init__(self):
        self.closed = False

    def fileno(self):
        return -1

    def sendall(self, data):
        pass

    def close(self):
        self.closed = True


def test_serve_client_reports_systemexit_to_stderr_instead_of_swallowing_it(
    monkeypatch, capsys
):
    def _raise(host, origin):
        raise SystemExit(
            "srw-ssh-proxy: token exchange refused (403): bad or unscoped PAT"
        )

    monkeypatch.setattr(proxy, "_connect", _raise)
    client = _FakeClient()
    proxy._serve_client(client, "api.srw.works", "https://cockpit.srw.works")
    err = capsys.readouterr().err
    assert "token exchange refused" in err
    # Doubled-prefix minor (findings-r2.md): the SystemExit's own message
    # already carries "srw-ssh-proxy:", so _serve_client must not prefix it
    # again into "srw-ssh-proxy: srw-ssh-proxy: ...".
    assert err.count("srw-ssh-proxy:") == 1
    assert client.closed is True


# ---------------------------------------------------------------------------
# Doubled prefix (findings-r2.md minor) — most SystemExits in this file
# already carry "srw-ssh-proxy:" in their own message; re-prefixing at the
# catch site doubled it.
# ---------------------------------------------------------------------------


def test_report_error_does_not_double_an_existing_prefix(capsys):
    proxy._report_error(SystemExit("srw-ssh-proxy: already prefixed"))
    assert capsys.readouterr().err == "srw-ssh-proxy: already prefixed\n"


def test_report_error_adds_the_prefix_when_missing(capsys):
    proxy._report_error(ValueError("no prefix here"))
    assert capsys.readouterr().err == "srw-ssh-proxy: no prefix here\n"


# ---------------------------------------------------------------------------
# N-7 — main()'s --stdio branch had no equivalent of I-1's guard: a refused
# handshake, a hostile/malformed frame (ValueError from parse_frames), or a
# broken pipe surfaced as a raw traceback instead of a clean stderr line.
# ---------------------------------------------------------------------------


def test_main_stdio_mode_reports_errors_cleanly_instead_of_a_traceback(
    monkeypatch, capsys
):
    def _raise(host, origin):
        raise SystemExit("srw-ssh-proxy: gateway closed during handshake")

    monkeypatch.setattr(proxy, "_connect", _raise)
    rc = proxy.main(["--stdio", "api.srw.works"])
    assert rc != 0
    err = capsys.readouterr().err
    assert "gateway closed during handshake" in err
    assert err.count("srw-ssh-proxy:") == 1


class _FakeStdioStream:
    """A stand-in for sys.stdin/sys.stdout with just enough surface for
    main()'s --stdio branch. pytest's own capture replaces sys.stdin with a
    pseudofile that has no real fileno(), so a test running under capsys
    needs this instead of the real sys.stdin/sys.stdout."""

    def __init__(self, fd):
        self._fd = fd

    def fileno(self):
        return self._fd


def test_main_stdio_mode_reports_a_bare_valueerror_cleanly(monkeypatch, capsys):
    """A hostile/malformed frame raises ValueError from parse_frames deep
    inside _pump; this must not be a raw traceback either, and (unlike the
    SystemExit case above) this exception has no existing prefix to begin
    with, exercising _report_error's other branch."""

    def _raise_pump(*_args, **_kwargs):
        raise ValueError("frame payload exceeds the cap")

    monkeypatch.setattr(proxy, "_connect", lambda host, origin: (object(), b""))
    monkeypatch.setattr(proxy, "_pump", _raise_pump)
    monkeypatch.setattr(proxy.sys, "stdin", _FakeStdioStream(0))
    monkeypatch.setattr(proxy.sys, "stdout", _FakeStdioStream(1))
    rc = proxy.main(["--stdio", "api.srw.works"])
    assert rc != 0
    err = capsys.readouterr().err
    assert err == "srw-ssh-proxy: frame payload exceeds the cap\n"


# ---------------------------------------------------------------------------
# I-4 — the outbound TLS socket was never closed in listener mode.
# ---------------------------------------------------------------------------


def test_serve_client_closes_the_outbound_socket_even_on_pump_failure(monkeypatch):
    closed = []

    class _FakeSock:
        def close(self):
            closed.append(True)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("relay exploded")

    monkeypatch.setattr(proxy, "_connect", lambda host, origin: (_FakeSock(), b""))
    monkeypatch.setattr(proxy, "_pump", _boom)
    client = _FakeClient()
    proxy._serve_client(client, "api.srw.works", "https://cockpit.srw.works")
    assert closed == [True]
    assert client.closed is True


# ---------------------------------------------------------------------------
# Minor — Ctrl-C in --listen mode used to exit by traceback.
# ---------------------------------------------------------------------------


def test_listen_mode_exits_cleanly_on_keyboardinterrupt(monkeypatch):
    class _FakeListener:
        def __init__(self):
            self.closed = False

        def setsockopt(self, *_args, **_kwargs):
            pass

        def bind(self, *_args, **_kwargs):
            pass

        def listen(self, *_args, **_kwargs):
            pass

        def accept(self):
            raise KeyboardInterrupt

        def close(self):
            self.closed = True

    fake_listener = _FakeListener()
    monkeypatch.setattr(proxy.socket, "socket", lambda *_args, **_kwargs: fake_listener)
    rc = proxy.main(["--listen", "127.0.0.1:0", "api.srw.works"])
    assert rc == 130
    assert fake_listener.closed is True
