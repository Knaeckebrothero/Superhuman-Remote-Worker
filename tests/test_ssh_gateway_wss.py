"""Tests for the SSH gateway's two front doors.

There are two transports, and they are NOT equally protected — the module
docstring says so and these tests pin it:

* **WSS** (``/api/ssh/attach``): an exact-match Origin check plus a
  user-bound bearer token, both applied before the upgrade.
* **TCP** (:2222): neither. Its controls are the LoadBalancer's
  ``allowedClientCIDRs`` (Task 11) and SSH public-key auth. There is no
  Origin header on a raw socket and no place to put a bearer token before
  the SSH banner.

Both hand their socket to the *same* ``GatewaySSHServer`` factory and charge
the *same* limiter, which is what keeps the two paths from drifting.

Three seams here have leaked in this plan before — ``detach`` never called,
an early return skipping a release, an upstream connection never closed — so
every release path below is asserted by count, not by "it didn't crash",
including the one where ``accept()`` raises.
"""

import asyncio
import errno
import logging
import socket
import subprocess
from types import SimpleNamespace

import pytest
from starlette.datastructures import Address, Headers

import ssh_gateway
from services.ssh_gateway_token import mint_attach_token
from ssh_gateway import client_ip, origin_allowed

SECRET = "test-only-session-secret"
USER = "00000000-0000-0000-0000-000000000001"


# ---------------------------------------------------------------------------
# origin_allowed
# ---------------------------------------------------------------------------


def test_exact_origin_is_allowed():
    assert (
        origin_allowed("https://cockpit.srw.works", ("https://cockpit.srw.works",))
        is True
    )


@pytest.mark.parametrize(
    "origin",
    [
        "https://evil.example",
        "https://cockpit.srw.works.evil.example",
        "http://cockpit.srw.works",  # scheme downgrade
        "https://cockpit.srw.works:8443",  # different port
        None,
        "",
        "null",
    ],
)
def test_everything_else_is_refused(origin):
    """Starlette performs no default same-origin check, unlike gorilla/websocket.
    Gitpod's CVE-2023-0957 was exactly this gap, and its payoff was permanent
    SSH persistence via addSSHPublicKey."""
    assert origin_allowed(origin, ("https://cockpit.srw.works",)) is False


def test_empty_allow_list_refuses_everything():
    assert origin_allowed("https://cockpit.srw.works", ()) is False


# ---------------------------------------------------------------------------
# client_ip — the rate limiter's key
# ---------------------------------------------------------------------------


def _ws(headers=None, host="203.0.113.9", app=None):
    return FakeWebSocket(headers=headers, host=host, app=app)


def test_forwarded_for_is_ignored_from_an_untrusted_peer():
    """The whole point. X-Forwarded-For is client-supplied; believing it
    unconditionally lets one source mint a fresh rate-limit bucket per
    connection and nullify every per-source control in GatewayLimiter."""
    ws = _ws({"x-forwarded-for": "10.0.0.1"}, host="203.0.113.9")
    assert client_ip(ws, ()) == "203.0.113.9"
    assert client_ip(ws, ("192.168.0.0/16",)) == "203.0.113.9"


def test_forwarded_for_is_believed_from_the_known_ingress_hop():
    ws = _ws({"x-forwarded-for": "198.51.100.7"}, host="10.42.3.9")
    assert client_ip(ws, ("10.42.0.0/16",)) == "198.51.100.7"


def test_a_spoofed_leading_entry_does_not_win():
    """ingress-nginx APPENDS the real peer to whatever the client sent, so
    the rightmost entry is the one the trusted hop wrote and the leftmost is
    whatever the client made up. The plan's original ``_client_ip`` took
    ``split(",")[0]`` — the attacker's half."""
    ws = _ws({"x-forwarded-for": "1.2.3.4, 198.51.100.7"}, host="10.42.3.9")
    assert client_ip(ws, ("10.42.0.0/16",)) == "198.51.100.7"


def test_a_trusted_hop_with_no_forwarded_header_falls_back_to_the_peer():
    ws = _ws({}, host="10.42.3.9")
    assert client_ip(ws, ("10.42.0.0/16",)) == "10.42.3.9"


def test_a_missing_peer_is_named_rather_than_empty():
    ws = _ws({}, host=None)
    assert client_ip(ws, ()) == "unknown"


def test_a_single_trusted_address_without_a_mask_works():
    ws = _ws({"x-forwarded-for": "198.51.100.7"}, host="10.42.3.9")
    assert client_ip(ws, ("10.42.3.9",)) == "198.51.100.7"


# ---------------------------------------------------------------------------
# the bearer token
# ---------------------------------------------------------------------------


def test_a_minted_token_authenticates_and_names_its_user(config):
    token, _ = mint_attach_token(USER, SECRET)
    ws = _ws({"authorization": f"Bearer {token}"})
    assert ssh_gateway.attach_principal(ws, config) == USER


def test_the_bearer_scheme_is_case_insensitive(config):
    token, _ = mint_attach_token(USER, SECRET)
    ws = _ws({"authorization": f"bearer {token}"})
    assert ssh_gateway.attach_principal(ws, config) == USER


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "Bearer ",
        "Basic dXNlcjpwYXNz",
        "Bearer not-a-token",
        # The credential this endpoint used to accept. It is the platform's
        # service-to-service key; presenting it must now buy nothing.
        "Bearer internal-key-value",
    ],
)
def test_everything_that_is_not_a_minted_token_is_refused(config, header):
    headers = {} if header is None else {"authorization": header}
    assert ssh_gateway.attach_principal(_ws(headers), config) is None


def test_the_internal_key_is_not_a_valid_user_credential(config):
    """Ruling G38, pinned. ``config.internal_key`` is MCP_INTERNAL_KEY: the
    value the gateway sends as X-Internal-Key to ~50 require_internal
    endpoints. The original `_token_valid` accepted exactly this."""
    ws = _ws({"authorization": f"Bearer {config.internal_key}"})
    assert config.internal_key
    assert ssh_gateway.attach_principal(ws, config) is None


# ---------------------------------------------------------------------------
# attach_endpoint — refusals, and the pre-auth slot
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_bad_origin_is_closed_before_the_upgrade(app):
    ws = _ws({"origin": "https://evil.example"}, app=app)
    await ssh_gateway.attach_endpoint(ws)
    assert ws.closed_code == ssh_gateway.WS_ORIGIN_REFUSED
    assert ws.accepted is False
    assert app.state.limiter.admits == []


@pytest.mark.asyncio
async def test_a_missing_token_is_closed_before_the_upgrade(app):
    ws = _ws({"origin": "https://cockpit.srw.works"}, app=app)
    await ssh_gateway.attach_endpoint(ws)
    assert ws.closed_code == ssh_gateway.WS_TOKEN_REFUSED
    assert ws.accepted is False
    assert app.state.limiter.admits == []


@pytest.mark.asyncio
async def test_the_origin_is_checked_before_the_token(app):
    """Cheapest check first, and it means a cross-site page cannot even
    probe token validity."""
    token, _ = mint_attach_token(USER, SECRET)
    ws = _ws(
        {"origin": "https://evil.example", "authorization": f"Bearer {token}"},
        app=app,
    )
    await ssh_gateway.attach_endpoint(ws)
    assert ws.closed_code == ssh_gateway.WS_ORIGIN_REFUSED


@pytest.mark.asyncio
async def test_a_refused_admission_closes_without_accepting_or_releasing(app):
    app.state.limiter.allow = False
    ws = _authorized_ws(app)
    await ssh_gateway.attach_endpoint(ws)
    assert ws.closed_code == ssh_gateway.WS_RATE_REFUSED
    assert ws.accepted is False
    # A release for a slot that was never granted would inflate the global
    # cap; GatewayLimiter guards it, but the gateway must not attempt it.
    assert app.state.limiter.releases == []


@pytest.mark.asyncio
async def test_an_accept_that_raises_still_releases_its_preauth_slot(app):
    """The leak the plan's original code shipped: ``await websocket.accept()``
    sat between ``try_admit()`` and the ``try`` block, so a raising accept
    (a client that vanishes during the handshake — routine on the internet)
    held its slot forever. 64 of those and the gateway refuses everyone
    until the pod restarts."""
    ws = _authorized_ws(app)
    ws.accept_error = RuntimeError("client vanished mid-handshake")

    with pytest.raises(RuntimeError):
        await ssh_gateway.attach_endpoint(ws)

    assert app.state.limiter.admits == ["203.0.113.9"]
    assert app.state.limiter.releases == ["203.0.113.9"]


@pytest.mark.asyncio
async def test_the_happy_path_speaks_ssh_and_releases_exactly_once(app):
    """End to end over the socketpair: a real asyncssh server answers a real
    client's version banner through the WebSocket pump, in both directions.

    This is the test that would have caught a pump wired to the wrong half
    of the socketpair, an ``_ACMWrapper`` handed to ``create_task``, or a
    ``run_server`` awaited before the client connects (which deadlocks:
    it does not return until authentication completes)."""
    client_sock, ws_sock = socket.socketpair()
    ws = _authorized_ws(app, transport=ws_sock)
    endpoint = asyncio.create_task(ssh_gateway.attach_endpoint(ws))

    loop = asyncio.get_running_loop()
    client_sock.setblocking(False)
    await loop.sock_sendall(client_sock, b"SSH-2.0-TestClient\r\n")
    banner = await asyncio.wait_for(loop.sock_recv(client_sock, 128), timeout=5)
    assert banner.startswith(b"SSH-2.0-"), banner

    client_sock.close()
    await asyncio.wait_for(endpoint, timeout=5)

    assert ws.accepted is True
    assert app.state.limiter.admits == ["203.0.113.9"]
    assert app.state.limiter.releases == ["203.0.113.9"]
    # Closed by us, not left for the ASGI server to tear down -- a client
    # otherwise observes an abnormal close rather than a clean one.
    assert ws.closed_code is not None


@pytest.mark.asyncio
async def test_the_server_is_built_with_the_real_client_ip(app, monkeypatch):
    """``get_extra_info('peername')`` on a socketpair is ``''``, so the only
    place the real client IP can come from is the factory closure. Without
    it every connection rate-limits and audits as the same empty string."""
    seen = []
    real = ssh_gateway.GatewaySSHServer

    def _spy(context, client_ip_value):
        seen.append(client_ip_value)
        return real(context, client_ip_value)

    monkeypatch.setattr(ssh_gateway, "GatewaySSHServer", _spy)

    client_sock, ws_sock = socket.socketpair()
    ws = _authorized_ws(app, transport=ws_sock)
    endpoint = asyncio.create_task(ssh_gateway.attach_endpoint(ws))
    loop = asyncio.get_running_loop()
    client_sock.setblocking(False)
    await loop.sock_sendall(client_sock, b"SSH-2.0-TestClient\r\n")
    await asyncio.wait_for(loop.sock_recv(client_sock, 128), timeout=5)
    client_sock.close()
    await asyncio.wait_for(endpoint, timeout=5)

    assert seen == ["203.0.113.9"]


# ---------------------------------------------------------------------------
# the TCP listener on 2222
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_tcp_listener_answers_with_an_ssh_banner(app):
    """Nothing in this plan bound 2222 before this, yet Task 11 ships a
    containerPort and a LoadBalancer on it and every step of Task 12's live
    gate is an `ssh -p 2222`. Without this the chart fronts a dead port."""
    async with ssh_gateway.lifespan(app):
        listener = app.state.ssh_listener
        assert listener is not None
        reader, writer = await asyncio.open_connection("127.0.0.1", listener.port)
        try:
            writer.write(b"SSH-2.0-TestClient\r\n")
            await writer.drain()
            banner = await asyncio.wait_for(reader.read(128), timeout=5)
            assert banner.startswith(b"SSH-2.0-"), banner
        finally:
            writer.close()


@pytest.mark.asyncio
async def test_the_tcp_listener_charges_and_releases_the_same_limiter(app):
    async with ssh_gateway.lifespan(app):
        listener = app.state.ssh_listener
        reader, writer = await asyncio.open_connection("127.0.0.1", listener.port)
        writer.write(b"SSH-2.0-TestClient\r\n")
        await writer.drain()
        await asyncio.wait_for(reader.read(128), timeout=5)
        assert app.state.limiter.admits == ["127.0.0.1"]
        writer.close()
        for _ in range(100):
            if app.state.limiter.releases:
                break
            await asyncio.sleep(0.02)
    assert app.state.limiter.releases == ["127.0.0.1"]


@pytest.mark.asyncio
async def test_a_tcp_connection_refused_by_the_limiter_never_speaks_ssh(app):
    """Refused before a single SSH byte is written, and with no slot
    charged — the pre-auth cap exists because asyncssh ships no MaxStartups
    (measured: 1000 silent pre-auth connections accepted in 0.3s)."""
    app.state.limiter.allow = False
    async with ssh_gateway.lifespan(app):
        listener = app.state.ssh_listener
        reader, writer = await asyncio.open_connection("127.0.0.1", listener.port)
        try:
            data = await asyncio.wait_for(reader.read(128), timeout=5)
            assert data == b""
        finally:
            writer.close()
    assert app.state.limiter.releases == []


@pytest.mark.asyncio
async def test_shutdown_closes_the_listening_socket(app):
    async with ssh_gateway.lifespan(app):
        port = app.state.ssh_listener.port
    assert app.state.ssh_listener is None
    with pytest.raises(OSError):
        sock = socket.create_connection(("127.0.0.1", port), timeout=2)
        sock.close()


@pytest.mark.asyncio
async def test_shutdown_drains_the_audit_background_tasks(app, monkeypatch):
    """Carried from Task 6: ``connection_lost`` schedules the attachment
    close rather than awaiting it, so without a drain a rollout leaves those
    rows open — silently, because the loop simply stops."""
    drained = []

    async def _drain(timeout=None):
        drained.append(timeout)
        return 2

    monkeypatch.setattr(ssh_gateway, "drain_background_tasks", _drain)
    async with ssh_gateway.lifespan(app):
        pass
    assert len(drained) == 1


# ---------------------------------------------------------------------------
# Fix round 1 (review findings 1, 3, 4, 5, 6)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_transient_accept_error_does_not_kill_the_listener(app, monkeypatch):
    """EMFILE must not be terminal.

    The worst failure shape in an orchestrated environment: one OSError from
    ``sock_accept`` used to end the accept loop for good, while ``/healthz``
    kept returning ok -- so Kubernetes never restarted the pod and SSH was
    simply gone with no signal. asyncio's own ``_accept_connection`` (the loop
    this raw one replaced) logs and continues on exactly EMFILE/ENFILE/
    ENOBUFS/ENOMEM; the regression came from replacing a battle-tested loop
    with a bare one.
    """
    loop = asyncio.get_running_loop()
    real_accept = loop.sock_accept
    calls = {"n": 0}

    async def flaky(sock):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(errno.EMFILE, "Too many open files")
        return await real_accept(sock)

    monkeypatch.setattr(loop, "sock_accept", flaky)

    async with ssh_gateway.lifespan(app):
        listener = app.state.ssh_listener
        reader, writer = await asyncio.open_connection("127.0.0.1", listener.port)
        try:
            writer.write(b"SSH-2.0-TestClient\r\n")
            await writer.drain()
            banner = await asyncio.wait_for(reader.read(128), timeout=5)
            assert banner.startswith(b"SSH-2.0-"), banner
            assert calls["n"] >= 2  # the EMFILE really was raised and survived
            assert listener.is_serving() is True
        finally:
            writer.close()


@pytest.mark.asyncio
async def test_health_reports_down_once_the_accept_loop_dies(app, monkeypatch):
    """If the listener does die, it must stop passing its health check.

    Covers the unexpected-exception path too (finding 3): whatever ends the
    loop, the pod must stop advertising itself as Ready with a dead 2222.
    """
    loop = asyncio.get_running_loop()

    async def fatal(sock):
        raise RuntimeError("something no OSError handler covers")

    monkeypatch.setattr(loop, "sock_accept", fatal)

    async with ssh_gateway.lifespan(app):
        listener = app.state.ssh_listener
        for _ in range(100):
            if not listener.is_serving():
                break
            await asyncio.sleep(0.01)
        assert listener.is_serving() is False
        response = await ssh_gateway.healthz(_request(app))
        assert response.status_code == 503


@pytest.mark.asyncio
async def test_health_is_ok_while_the_listener_serves(app):
    """The negative control for the test above: a healthy gateway says so."""
    async with ssh_gateway.lifespan(app):
        response = await ssh_gateway.healthz(_request(app))
        assert response.status_code == 200


@pytest.mark.asyncio
async def test_a_connection_task_cancelled_before_it_runs_still_releases(app):
    """The slot is released from the task's done callback, not from a
    ``finally`` inside the coroutine: a task cancelled before its first
    scheduling never executes a line of its body, so an inner ``finally``
    never runs and the slot leaks. Shutdown-only, but the seam stays uniform.
    """
    async with ssh_gateway.lifespan(app):
        listener = app.state.ssh_listener
        left, right = socket.socketpair()
        try:
            task = listener._spawn(right, "198.51.100.5")
            task.cancel()  # before the event loop ever schedules it
            await asyncio.gather(task, return_exceptions=True)
            assert app.state.limiter.releases == ["198.51.100.5"]
        finally:
            left.close()


@pytest.mark.asyncio
async def test_a_refused_origin_is_metered_against_the_source(app):
    """Unauthenticated handshake floods were not metered at all: only a
    client that got PAST the Origin check and the bearer token was ever rate
    limited (finding 5). The meter charges the rate window only -- charging a
    concurrency slot for a handshake that never opened would never be
    released, and a bad-origin flood would lock out everyone."""
    ws = _ws({"origin": "https://evil.example"}, app=app)
    await ssh_gateway.attach_endpoint(ws)
    assert app.state.limiter.refusals == ["203.0.113.9"]
    assert app.state.limiter.admits == []
    assert app.state.limiter.releases == []


@pytest.mark.asyncio
async def test_a_refused_token_is_metered_against_the_source(app):
    """The token path matters more than the Origin path: it is the one that
    runs an HMAC per attempt."""
    ws = _ws({"origin": "https://cockpit.srw.works"}, app=app)
    await ssh_gateway.attach_endpoint(ws)
    assert app.state.limiter.refusals == ["203.0.113.9"]
    assert app.state.limiter.admits == []


@pytest.mark.asyncio
async def test_a_refusal_over_budget_is_still_refused_not_admitted(app):
    """Over-budget only changes the log line: the handshake was already
    refused, and the meter must never turn into a second admission path."""
    app.state.limiter.note_handshake_refusal = lambda ip: False
    ws = _ws({"origin": "https://evil.example"}, app=app)
    await ssh_gateway.attach_endpoint(ws)
    assert ws.closed_code == ssh_gateway.WS_ORIGIN_REFUSED
    assert ws.accepted is False
    assert app.state.limiter.admits == []


@pytest.mark.asyncio
async def test_a_refused_handshake_closes_the_socket_without_metering_a_slot(app):
    """Rate-limit refusals (past origin and token) are NOT re-metered: they
    were already counted by ``try_admit``'s own window."""
    app.state.limiter.allow = False
    ws = _authorized_ws(app)
    await ssh_gateway.attach_endpoint(ws)
    assert app.state.limiter.refusals == []
    assert ws.closed_code == ssh_gateway.WS_RATE_REFUSED


def _request(app):
    return SimpleNamespace(app=app)


# ---------------------------------------------------------------------------
# wiring
# ---------------------------------------------------------------------------


def test_the_module_imports_without_any_environment():
    """No module-level ``app = _build_app()``: importing this module must not
    require host keys, a CA or a secret, or the tests above could not import
    it at all."""
    assert not hasattr(ssh_gateway, "app")


def test_create_app_binds_every_context_callable(app):
    context = app.state.context
    assert context.missing_required() == ()
    # All three audit callables, not None. ``mark_key_used`` in particular was
    # left defaulted to None by Task 6, so ``last_used_at`` never moved in
    # production despite the whole machinery being in place (ruling G1).
    assert context.record_attach is not None
    assert context.close_attach is not None
    assert context.mark_key_used is not None


@pytest.mark.asyncio
async def test_the_bound_callables_carry_the_config_and_the_right_arity(
    app, monkeypatch
):
    """``GatewayContext``'s callables are pre-bound to the config, so a
    mis-ordered partial would only surface at runtime, on the audit path,
    where every exception is swallowed by design.

    The doubles replace the names ``ssh_gateway`` itself imported, not the
    ones in ``services.ssh_gateway_client``: ``from x import y`` binds at
    import time, so patching the source module would leave ``_build_app``
    partialling the real HTTP client — which is exactly what happened the
    first time this test ran, and it dialled ``http://orchestrator:8085``.
    """
    calls = {}

    async def _mark_key_used(config, fingerprint):
        calls["mark"] = (config, fingerprint)

    async def _record(config, fingerprint, handle, client_ip_value=None):
        calls["record"] = (config, fingerprint, handle, client_ip_value)
        return "att-1"

    async def _close(config, attachment_id, fingerprint, channels=()):
        calls["close"] = (config, attachment_id, fingerprint, list(channels))
        return 1

    async def _resolve(config, handle, fingerprint):
        calls["resolve"] = (config, handle, fingerprint)
        return "target"

    monkeypatch.setattr(ssh_gateway, "mark_key_used", _mark_key_used)
    monkeypatch.setattr(ssh_gateway, "record_attachment", _record)
    monkeypatch.setattr(ssh_gateway, "close_attachment", _close)
    monkeypatch.setattr(ssh_gateway, "resolve_target", _resolve)
    rebuilt = ssh_gateway.create_app()
    context = rebuilt.state.context

    await context.mark_key_used("SHA256:fp")
    await context.record_attach("SHA256:fp", "s-7f3a91c2", "203.0.113.9")
    await context.close_attach("att-1", "SHA256:fp", ["session"])
    await context.resolve("s-7f3a91c2", "SHA256:fp")

    assert calls["mark"][1] == "SHA256:fp"
    assert calls["record"][1:] == ("SHA256:fp", "s-7f3a91c2", "203.0.113.9")
    assert calls["close"][1:] == ("att-1", "SHA256:fp", ["session"])
    assert calls["resolve"][1:] == ("s-7f3a91c2", "SHA256:fp")
    for key in ("mark", "record", "close", "resolve"):
        assert calls[key][0] is rebuilt.state.config


def test_healthz_is_the_only_unauthenticated_route(app):
    paths = {getattr(route, "path", None) for route in app.routes}
    assert paths == {"/healthz", "/api/ssh/attach"}


# ---------------------------------------------------------------------------
# fixtures and doubles
# ---------------------------------------------------------------------------


class RecordingLimiter:
    """Wraps the real GatewayLimiter's contract, recording every charge.

    A counting double rather than the real limiter because the properties
    under test are "released exactly once, for the right key" — which a real
    limiter's internal dict can only show indirectly.
    """

    def __init__(self):
        self.allow = True
        self.admits = []
        self.releases = []
        self.refusals = []

    def note_handshake_refusal(self, client_ip_value):
        self.refusals.append(client_ip_value)
        return True

    def try_admit(self, client_ip_value):
        if not self.allow:
            return False
        self.admits.append(client_ip_value)
        return True

    def release(self, client_ip_value):
        self.releases.append(client_ip_value)

    def try_open_channel(self, connection_id):
        return True

    def close_channel(self, connection_id):
        pass

    def try_attach(self, workspace_id):
        return True

    def detach(self, workspace_id):
        pass


class FakeWebSocket:
    """The subset of Starlette's WebSocket that ``attach_endpoint`` touches.

    ``transport``, when given, is one half of a socketpair whose other half
    the test drives — which turns this double into a real byte pipe and lets
    a real SSH handshake run through the endpoint.
    """

    def __init__(self, headers=None, host="203.0.113.9", app=None, transport=None):
        self.headers = Headers(headers or {})
        self.client = Address(host, 51234) if host else None
        self.app = app
        self.accepted = False
        self.accept_error = None
        self.closed_code = None
        self._transport = transport
        if transport is not None:
            transport.setblocking(False)

    async def accept(self, subprotocol=None, headers=None):
        if self.accept_error is not None:
            raise self.accept_error
        self.accepted = True

    async def close(self, code=1000, reason=None):
        self.closed_code = code

    async def receive_bytes(self):
        loop = asyncio.get_running_loop()
        data = await loop.sock_recv(self._transport, 65536)
        if not data:
            raise ConnectionResetError("test websocket closed")
        return data

    async def send_bytes(self, data):
        loop = asyncio.get_running_loop()
        await loop.sock_sendall(self._transport, data)


def _authorized_ws(app, transport=None):
    token, _ = mint_attach_token(USER, SECRET)
    return FakeWebSocket(
        headers={
            "origin": "https://cockpit.srw.works",
            "authorization": f"Bearer {token}",
        },
        app=app,
        transport=transport,
    )


@pytest.fixture(scope="session")
def gateway_env(tmp_path_factory):
    """A real Ed25519 host key and CA key: ``load_config`` parses both."""
    key_dir = tmp_path_factory.mktemp("ssh_gateway_wss")
    host_key = key_dir / "host_ed25519"
    ca_key = key_dir / "user_ca"
    for path in (host_key, ca_key):
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(path)],
            check=True,
            capture_output=True,
        )
    return {
        "SSH_GATEWAY_HOST_KEYS": str(host_key),
        "SSH_GATEWAY_USER_CA": str(ca_key),
        "ORCHESTRATOR_URL": "http://orchestrator:8085",
        "MCP_INTERNAL_KEY": "internal-key-value",
        "SSH_GATEWAY_ALLOWED_ORIGINS": "https://cockpit.srw.works",
        "SESSION_JWT_SECRET": SECRET,
        # Required at boot since fix round 2, and "none" is the honest answer
        # here: nothing proxies this test gateway, so the socket peer is the
        # source. The trusted-hop path is exercised by ``client_ip``'s own
        # tests above, which drive the header directly.
        "SSH_GATEWAY_TRUSTED_PROXIES": "none",
        # Ephemeral: 2222 would collide with anything else on the host.
        "SSH_GATEWAY_SSH_PORT": "0",
        "SSH_GATEWAY_SSH_HOST": "127.0.0.1",
    }


@pytest.fixture
def gateway_environment(gateway_env, monkeypatch):
    for key, value in gateway_env.items():
        monkeypatch.setenv(key, value)
    return gateway_env


@pytest.fixture
def config(gateway_environment):
    from services.ssh_gateway_config import load_config

    return load_config()


@pytest.fixture
def app(gateway_environment):
    application = ssh_gateway.create_app()
    application.state.limiter = RecordingLimiter()
    application.state.context.limiter = application.state.limiter
    return application


def test_info_records_survive_app_construction(gateway_environment):
    """The gateway's refusal reasons must not be filtered out in production.

    Nothing in this process configures the root logger — uvicorn's dictConfig
    sets up only its own ``uvicorn.*`` loggers — so root is left with no
    handler and Python falls back to ``logging.lastResort``, which is
    WARNING-level. Warnings and tracebacks still surface, which is exactly why
    this is easy to miss; what silently disappears is every INFO record, and on
    this gateway that is the refusal path: "refused wss handshake from %s
    (%s)", "refused tcp connection from %s", "refusing a subsystem channel".
    Those are the first thing anyone asks for when a user reports that SSH will
    not connect.

    Asserts on the logger's effective level rather than on captured output,
    because pytest's own capture plumbing attaches handlers and would mask the
    very condition under test.
    """
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    try:
        root.handlers.clear()
        root.setLevel(logging.WARNING)  # a fresh interpreter's default

        ssh_gateway.create_app()

        gateway_logger = logging.getLogger(ssh_gateway.__name__)
        assert gateway_logger.isEnabledFor(logging.INFO), (
            "INFO is filtered, so every refusal reason is dropped: an operator "
            "sees a failed connection with no logged cause"
        )
        assert root.handlers, "no root handler: records reach lastResort at WARNING"
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
