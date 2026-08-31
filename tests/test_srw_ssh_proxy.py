import http.client
import importlib.util
import os
import pathlib
import socket
import ssl
import threading
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

        # Local -> remote. A PING may also be queued on the very first loop
        # iteration (last_ping starts at 0.0, and time.monotonic() doesn't),
        # so filter for the BINARY frame rather than assuming it's first.
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
    assert "token exchange refused" in capsys.readouterr().err
    assert client.closed is True


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
