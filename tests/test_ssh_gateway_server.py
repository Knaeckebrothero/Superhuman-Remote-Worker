"""Tests for the SSH gateway's server callbacks.

Three properties here are *carries*: they are correct only because this
module calls something a sibling module built and nothing else ever does.
Each has a negative control recorded in its docstring, and each was
confirmed to fail when its single line of wiring is removed:

1. ``validate_public_key`` narrows the presented key's own signature
   algorithms. ``signature_algs`` is advisory on asyncssh's server side --
   ``SSHKey.verify()`` gates on the KEY's ``all_sig_algorithms``
   (``public_key.py:587``) -- so without the narrowing call an RSA user key
   authenticates with SHA-1 ``ssh-rsa``.
2. ``auth_completed`` bumps ``last_used_at`` over HTTP. Without it the
   column never moves and the stolen-key signal it exists for is dead.
3. ``session_requested``/channel close charge and release the limiter's
   per-connection channel cap. Without it a client opens 300 channels on
   one connection and exhausts the workspace sshd's ``MaxSessions 16``.

``GatewayContext``'s callables are pre-bound to the config (Task 8 builds
them with ``functools.partial``), which is why the fakes below take no
config argument -- matching the brief's own ``_resolver(handle,
fingerprint)`` shape.
"""

import asyncio

import asyncssh
import pytest
from asyncssh.constants import OPEN_ADMINISTRATIVELY_PROHIBITED
from asyncssh.sftp import MIN_SFTP_VERSION

import services.ssh_gateway_server as mod
from services.ssh_gateway_client import (
    REFUSAL_MESSAGES,
    SshTarget,
    TargetDenied,
    TargetUnavailable,
)
from services.ssh_gateway_limits import GatewayLimiter
from services.ssh_gateway_server import (
    GatewayContext,
    GatewaySSHServer,
    clamp_direct_tcpip,
)

HANDLE = "s-7f3a91c2"
CLIENT_IP = "203.0.113.9"


def _context(**overrides) -> GatewayContext:
    base = dict(
        config=None,
        ca=None,
        limiter=None,
        resolve=None,
        record_attach=None,
        close_attach=None,
    )
    base.update(overrides)
    return GatewayContext(**base)


def _limiter(**overrides) -> GatewayLimiter:
    base = dict(
        max_preauth_connections=64,
        preauth_rate_per_minute=60,
        max_channels_per_connection=3,
        max_attachments_per_workspace=2,
    )
    base.update(overrides)
    return GatewayLimiter(**base)


def _target(**overrides) -> SshTarget:
    base = dict(
        thread_id="t1",
        user_id="u1",
        pod_ip="workspace-t1.ns.svc.cluster.local",
        pod_port=22,
        host_key_fingerprint="SHA256:host",
        state="live",
    )
    base.update(overrides)
    return SshTarget(**base)


class FakeProcess:
    """The shape ``_session_factory`` reads off an asyncssh SSHServerProcess.

    Only ``subsystem`` (for the audit channel-type record), ``stderr.write``
    and ``exit`` are touched on a refusal path, which is all this task's
    ``_upstream`` can reach -- the real dial lands with Task 7.
    """

    def __init__(self, subsystem=None):
        self.subsystem = subsystem
        self.command = None
        self.stderr = self
        self.written = []
        self.exit_code = None

    def write(self, data):
        self.written.append(data)

    def exit(self, code):
        self.exit_code = code

    @property
    def stderr_text(self) -> str:
        return b"".join(self.written).decode()


def _authenticated(server: GatewaySSHServer, fingerprint="SHA256:abc"):
    """Put a server into the state auth_completed leaves it in."""
    server.handle = HANDLE
    server.presented_fingerprint = fingerprint
    return server


async def _drain_background_tasks() -> None:
    """``connection_lost`` is a synchronous asyncssh callback, so the audit
    close it needs is scheduled rather than awaited. Awaiting the scheduled
    task is what makes these assertions deterministic instead of
    sleep-and-hope."""
    pending = [task for task in list(mod._BACKGROUND_TASKS) if not task.done()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


# --- clamp_direct_tcpip ---------------------------------------------------


def test_loopback_is_allowed():
    assert clamp_direct_tcpip("127.0.0.1") is True
    assert clamp_direct_tcpip("localhost") is True


@pytest.mark.parametrize(
    "dest",
    ["10.1.2.3", "0.0.0.0", "example.com", "169.254.169.254", "::1", ""],
)
def test_everything_else_is_refused(dest):
    """The workspace sshd enforces PermitOpen 127.0.0.1:* anyway, but refusing
    early gives a better error and keeps the gateway's own posture legible.
    ::1 is refused because the workspace listens on IPv4 loopback only."""
    assert clamp_direct_tcpip(dest) is False


# --- authentication -------------------------------------------------------


@pytest.mark.asyncio
async def test_upstream_is_not_dialled_during_auth():
    """Callback order is begin_auth -> validate_public_key. Resolving a target
    in begin_auth was measured at 100 upstream dials for 100 failed auths."""
    dials = []

    async def _resolver(handle, fingerprint):
        dials.append(handle)
        raise AssertionError("resolver must not run during authentication")

    server = GatewaySSHServer(_context(resolve=_resolver), CLIENT_IP)
    assert server.begin_auth(HANDLE) is True
    assert dials == []


def test_public_key_auth_records_the_fingerprint():
    key = asyncssh.generate_private_key("ssh-ed25519")
    server = GatewaySSHServer(_context(), CLIENT_IP)
    assert server.validate_public_key(HANDLE, key.convert_to_public()) is True
    assert server.presented_fingerprint == key.convert_to_public().get_fingerprint(
        "sha256"
    )
    assert server.public_key_auth_supported() is True


def test_password_auth_is_never_offered():
    server = GatewaySSHServer(_context(), CLIENT_IP)
    assert server.password_auth_supported() is False
    assert server.kbdint_auth_supported() is False


def test_remote_forwarding_is_refused():
    server = GatewaySSHServer(_context(), CLIENT_IP)
    assert server.server_requested("0.0.0.0", 8080) is False


def test_a_malformed_handle_is_refused_at_key_validation():
    """begin_auth deliberately accepts any username (it must not resolve
    anything); validate_public_key is where a handle that could never
    address a session stops."""
    key = asyncssh.generate_private_key("ssh-ed25519").convert_to_public()
    server = GatewaySSHServer(_context(), CLIENT_IP)
    assert server.validate_public_key("../../admin", key) is False
    assert server.presented_fingerprint is None


# --- CARRY 1: signature-algorithm narrowing -------------------------------


def test_validate_public_key_narrows_an_rsa_key_against_sha1():
    """CARRY 1, with its negative control immediately below.

    ``signature_algs`` is advisory server-side: ``SSHServerConnection.
    validate_public_key`` calls ``key.verify()`` (``connection.py:6217``) on
    the very object handed to this callback, and ``SSHKey.verify()`` checks
    only that key's own ``all_sig_algorithms`` (``public_key.py:587``).
    Removing the ``narrow_signature_algorithms(key)`` line from
    ``validate_public_key`` makes the first assertion below flip to True --
    i.e. this test fails -- because an unnarrowed RSA key verifies a SHA-1
    ``ssh-rsa`` signature exactly as readily as ``rsa-sha2-256``
    (test_unnarrowed_rsa_key_is_the_negative_control proves that directly).
    """
    key = asyncssh.generate_private_key("ssh-rsa", key_size=3072)
    pub = key.convert_to_public()
    data = b"session-id-and-auth-message"

    server = GatewaySSHServer(_context(), CLIENT_IP)
    assert server.validate_public_key(HANDLE, pub) is True

    assert pub.verify(data, key.sign(data, b"ssh-rsa")) is False
    assert pub.verify(data, key.sign(data, b"rsa-sha2-256")) is True


def test_unnarrowed_rsa_key_is_the_negative_control():
    """The same key, never passed through validate_public_key, accepts SHA-1.
    This is what makes the assertion above discriminating rather than a
    tautology about RSA keys in general."""
    key = asyncssh.generate_private_key("ssh-rsa", key_size=3072)
    pub = key.convert_to_public()
    data = b"session-id-and-auth-message"

    assert pub.verify(data, key.sign(data, b"ssh-rsa")) is True


def test_narrowing_leaves_an_ed25519_key_usable():
    """Narrowing must not be so aggressive it breaks the algorithm this
    gateway actually expects: an Ed25519 key has exactly one signature
    algorithm and SIGNATURE_ALGS keeps it."""
    key = asyncssh.generate_private_key("ssh-ed25519")
    pub = key.convert_to_public()
    data = b"session-id-and-auth-message"

    server = GatewaySSHServer(_context(), CLIENT_IP)
    assert server.validate_public_key(HANDLE, pub) is True
    assert pub.verify(data, key.sign(data, b"ssh-ed25519")) is True


def test_validate_public_key_fails_closed_if_narrowing_cannot_be_applied():
    """narrow_signature_algorithms raises AssertionError if a future asyncssh
    stops honouring the assignment. That must become a refusal, not an
    exception escaping into asyncssh's auth path and certainly not an
    accepted key that still speaks SHA-1."""

    class _Unnarrowable:
        sig_algorithms = (b"ssh-rsa", b"rsa-sha2-256")
        all_sig_algorithms = frozenset({b"ssh-rsa", b"rsa-sha2-256"})

        def get_fingerprint(self, hash_name):
            return "SHA256:abc"

        def __setattr__(self, name, value):
            pass

    server = GatewaySSHServer(_context(), CLIENT_IP)
    assert server.validate_public_key(HANDLE, _Unnarrowable()) is False
    assert server.presented_fingerprint is None


# --- CARRY 2: last_used_at bump ------------------------------------------


@pytest.mark.asyncio
async def test_auth_completed_bumps_last_used_at():
    """CARRY 2. Deleting the ``await bump(...)`` line leaves ``bumped``
    empty and this test fails. The bump is keyed by fingerprint because
    target resolution is lazy -- at this instant the gateway holds nothing
    else."""
    bumped = []

    async def _bump(fingerprint):
        bumped.append(fingerprint)

    server = _authenticated(GatewaySSHServer(_context(mark_key_used=_bump), CLIENT_IP))
    await server.auth_completed()

    assert bumped == ["SHA256:abc"]


@pytest.mark.asyncio
async def test_a_failing_bump_never_tears_down_an_authenticated_session():
    async def _bump(fingerprint):
        raise RuntimeError("orchestrator is down")

    server = _authenticated(GatewaySSHServer(_context(mark_key_used=_bump), CLIENT_IP))
    await server.auth_completed()  # must not raise


@pytest.mark.asyncio
async def test_the_bump_is_skipped_without_a_fingerprint():
    """auth_completed cannot fire before a key verified, but a context with
    no bump callable (or an unset fingerprint) must be inert rather than a
    TypeError inside asyncssh's userauth-success path."""
    bumped = []

    async def _bump(fingerprint):
        bumped.append(fingerprint)

    server = GatewaySSHServer(_context(mark_key_used=_bump), CLIENT_IP)
    await server.auth_completed()
    assert bumped == []


# --- CARRY 3: per-connection channel cap ----------------------------------


def test_session_open_is_capped_per_connection():
    """CARRY 3. ``max_channels_per_connection=3`` here; the fourth session
    open must be refused. Removing ``try_open_channel`` from
    ``session_requested`` makes the fourth call return a process instead of
    raising, and this test fails.

    Refused with OPEN_ADMINISTRATIVELY_PROHIBITED rather than ``return
    False`` so the client sees a reason instead of a bare 'Session
    refused'."""
    limiter = _limiter()
    server = GatewaySSHServer(_context(limiter=limiter), CLIENT_IP)

    processes = [server.session_requested() for _ in range(3)]
    assert all(p is not None for p in processes)

    with pytest.raises(asyncssh.ChannelOpenError) as excinfo:
        server.session_requested()
    assert excinfo.value.code == OPEN_ADMINISTRATIVELY_PROHIBITED


def test_closing_a_channel_returns_its_slot():
    """The cap is concurrent, not cumulative: a client that opens and closes
    sessions serially must never be throttled."""
    limiter = _limiter()
    server = GatewaySSHServer(_context(limiter=limiter), CLIENT_IP)

    for _ in range(10):
        process = server.session_requested()
        process.connection_lost(None)

    assert server.session_requested() is not None


def test_a_channel_slot_is_released_only_once():
    """asyncssh's channel cleanup calls connection_lost once, but a double
    call must not free a slot this connection does not hold -- that would
    let one connection mint extra capacity for itself."""
    limiter = _limiter()
    server = GatewaySSHServer(_context(limiter=limiter), CLIENT_IP)

    first = server.session_requested()
    server.session_requested()
    server.session_requested()
    first.connection_lost(None)
    first.connection_lost(None)

    server.session_requested()
    with pytest.raises(asyncssh.ChannelOpenError):
        server.session_requested()


def test_connection_lost_frees_every_channel_slot_still_held():
    """A channel opened and never closed (client vanished mid-session) must
    not leak a slot into the limiter's per-connection dict. The limiter
    deletes an entry the moment its count reaches zero, so an empty
    ``_channels`` is the observable proof."""
    limiter = _limiter()
    server = GatewaySSHServer(_context(limiter=limiter), CLIENT_IP)

    server.session_requested()
    server.session_requested()
    server.connection_lost(None)

    assert limiter._channels == {}


def test_session_requested_uses_asyncssh_min_sftp_version():
    """MIN_SFTP_VERSION lives in asyncssh.sftp, not asyncssh.constants."""
    server = GatewaySSHServer(_context(limiter=_limiter()), CLIENT_IP)
    process = server.session_requested()
    assert process._sftp_version == MIN_SFTP_VERSION
    assert MIN_SFTP_VERSION == 3


# --- direct-tcpip ---------------------------------------------------------


def test_connection_requested_refuses_before_reaching_the_forwarder():
    """_forward_connection is a Task 7 stub that raises NotImplementedError.
    A non-loopback destination must be refused by the clamp *before* it, so
    this returning False (rather than raising) is what proves the clamp
    actually guards the stub."""
    server = GatewaySSHServer(_context(), CLIENT_IP)
    assert server.connection_requested("169.254.169.254", 80, "orig", 1) is False


def test_connection_requested_reaches_the_forwarder_for_loopback():
    """The other half of the test above: without this, "returns False" would
    be indistinguishable from "the clamp refuses everything"."""
    server = GatewaySSHServer(_context(), CLIENT_IP)
    with pytest.raises(NotImplementedError):
        server.connection_requested("127.0.0.1", 8080, "orig", 1)


# --- lazy resolution, attachment audit, attachment cap --------------------


@pytest.mark.asyncio
async def test_the_first_channel_resolves_and_records_an_attachment():
    resolved = []
    recorded = []

    async def _resolve(handle, fingerprint):
        resolved.append((handle, fingerprint))
        return _target()

    async def _record(fingerprint, handle, client_ip):
        recorded.append((fingerprint, handle, client_ip))
        return "att-1"

    limiter = _limiter()
    server = _authenticated(
        GatewaySSHServer(
            _context(limiter=limiter, resolve=_resolve, record_attach=_record),
            CLIENT_IP,
        )
    )

    target = await server._attached_target()

    assert target.thread_id == "t1"
    assert resolved == [(HANDLE, "SHA256:abc")]
    assert recorded == [("SHA256:abc", HANDLE, CLIENT_IP)]
    assert server.attachment_id == "att-1"


@pytest.mark.asyncio
async def test_a_second_channel_reuses_the_first_resolution():
    """One attachment row and one workspace-cap charge per CONNECTION, not
    per channel -- otherwise a JetBrains client (which opens several
    channels at once) would exhaust max_attachments_per_workspace by
    itself."""
    resolved = []
    recorded = []

    async def _resolve(handle, fingerprint):
        resolved.append(handle)
        return _target()

    async def _record(fingerprint, handle, client_ip):
        recorded.append(handle)
        return "att-1"

    limiter = _limiter()
    server = _authenticated(
        GatewaySSHServer(
            _context(limiter=limiter, resolve=_resolve, record_attach=_record),
            CLIENT_IP,
        )
    )

    await asyncio.gather(server._attached_target(), server._attached_target())

    assert len(resolved) == 1
    assert len(recorded) == 1
    assert limiter._attachments == {"t1": 1}


@pytest.mark.asyncio
async def test_a_failing_attachment_write_does_not_refuse_the_session():
    """Audit is bookkeeping. A control-plane hiccup on the audit row must
    not cost the user a session that already authenticated and resolved."""

    async def _resolve(handle, fingerprint):
        return _target()

    async def _record(fingerprint, handle, client_ip):
        raise RuntimeError("orchestrator is down")

    server = _authenticated(
        GatewaySSHServer(
            _context(limiter=_limiter(), resolve=_resolve, record_attach=_record),
            CLIENT_IP,
        )
    )

    target = await server._attached_target()
    assert target.thread_id == "t1"
    assert server.attachment_id is None


@pytest.mark.asyncio
async def test_the_workspace_attachment_cap_refuses_with_a_readable_reason():
    """max_attachments_per_workspace=2 here. The third *connection* to the
    same workspace is refused, and the refusal reaches stderr with a
    retryable exit code rather than a bare channel failure."""

    async def _resolve(handle, fingerprint):
        return _target()

    limiter = _limiter()
    servers = [
        _authenticated(
            GatewaySSHServer(_context(limiter=limiter, resolve=_resolve), CLIENT_IP)
        )
        for _ in range(3)
    ]
    await servers[0]._attached_target()
    await servers[1]._attached_target()

    with pytest.raises(mod.AttachmentLimitReached):
        await servers[2]._attached_target()

    process = FakeProcess()
    await servers[2]._session_factory(process)
    assert "too many" in process.stderr_text
    assert process.exit_code == mod.ATTACHMENT_LIMIT_EXIT_CODE


@pytest.mark.asyncio
async def test_connection_lost_closes_the_attachment_and_detaches():
    closed = []

    async def _resolve(handle, fingerprint):
        return _target()

    async def _record(fingerprint, handle, client_ip):
        return "att-1"

    async def _close(attachment_id, fingerprint, channels):
        closed.append((attachment_id, fingerprint, list(channels)))
        return 1

    limiter = _limiter()
    server = _authenticated(
        GatewaySSHServer(
            _context(
                limiter=limiter,
                resolve=_resolve,
                record_attach=_record,
                close_attach=_close,
            ),
            CLIENT_IP,
        )
    )
    await server._attached_target()
    # Task 7 owns the actual dial; _upstream raising here is the seam, and
    # the channel type must already be recorded by the time it does.
    with pytest.raises(NotImplementedError):
        await server._session_factory(FakeProcess(subsystem="sftp"))

    server.connection_lost(None)
    await _drain_background_tasks()

    assert closed == [("att-1", "SHA256:abc", ["sftp"])]
    assert limiter._attachments == {}


@pytest.mark.asyncio
async def test_a_failing_attachment_close_is_swallowed():
    async def _resolve(handle, fingerprint):
        return _target()

    async def _record(fingerprint, handle, client_ip):
        return "att-1"

    async def _close(attachment_id, fingerprint, channels):
        raise RuntimeError("orchestrator is down")

    limiter = _limiter()
    server = _authenticated(
        GatewaySSHServer(
            _context(
                limiter=limiter,
                resolve=_resolve,
                record_attach=_record,
                close_attach=_close,
            ),
            CLIENT_IP,
        )
    )
    await server._attached_target()

    server.connection_lost(None)
    await _drain_background_tasks()

    # The workspace slot is freed regardless of whether the audit row closed.
    assert limiter._attachments == {}


def test_connection_lost_without_an_attachment_is_inert():
    """Most connections die during or just after auth, having resolved
    nothing. That path must not schedule a close for an attachment id that
    was never issued."""
    server = GatewaySSHServer(_context(limiter=_limiter()), CLIENT_IP)
    server.connection_lost(None)


# --- refusal messages on the session channel ------------------------------


@pytest.mark.asyncio
async def test_a_denied_target_is_refused_with_a_readable_reason():
    """The whole reason authorization happens at channel open rather than at
    auth: the user reads why, instead of 'Permission denied (publickey)'."""

    async def _resolve(handle, fingerprint):
        raise TargetDenied()

    server = _authenticated(
        GatewaySSHServer(_context(limiter=_limiter(), resolve=_resolve), CLIENT_IP)
    )
    process = FakeProcess()
    await server._session_factory(process)

    message, code = REFUSAL_MESSAGES["denied"]
    assert process.stderr_text == f"srw: {message}\n"
    assert process.exit_code == code


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["suspended", "reclaimed", "stale_binding", "failed"])
async def test_each_unavailable_state_keeps_its_own_message_and_exit_code(state):
    async def _resolve(handle, fingerprint):
        raise TargetUnavailable(state)

    server = _authenticated(
        GatewaySSHServer(_context(limiter=_limiter(), resolve=_resolve), CLIENT_IP)
    )
    process = FakeProcess()
    await server._session_factory(process)

    message, code = REFUSAL_MESSAGES[state]
    assert process.stderr_text == f"srw: {message}\n"
    assert process.exit_code == code


@pytest.mark.asyncio
async def test_an_unknown_state_falls_back_to_unreachable():
    """REFUSAL_MESSAGES is keyed by workspace state and the orchestrator can
    grow a new one; an unmapped state must not KeyError inside a channel
    callback."""

    async def _resolve(handle, fingerprint):
        raise TargetUnavailable("a-state-nobody-has-written-yet")

    server = _authenticated(
        GatewaySSHServer(_context(limiter=_limiter(), resolve=_resolve), CLIENT_IP)
    )
    process = FakeProcess()
    await server._session_factory(process)

    message, code = REFUSAL_MESSAGES["unreachable"]
    assert process.stderr_text == f"srw: {message}\n"
    assert process.exit_code == code


@pytest.mark.asyncio
async def test_the_channel_type_is_recorded_before_the_upstream_is_tried():
    """The audit row's ``channels`` must reflect what the client asked for
    even when the attach was refused -- 'they tried to open sftp and were
    turned away' is exactly what the table is for."""

    async def _resolve(handle, fingerprint):
        raise TargetDenied()

    server = _authenticated(
        GatewaySSHServer(_context(limiter=_limiter(), resolve=_resolve), CLIENT_IP)
    )
    await server._session_factory(FakeProcess(subsystem="sftp"))
    await server._session_factory(FakeProcess())
    await server._session_factory(FakeProcess(subsystem="sftp"))

    assert server.channel_types == ["sftp", "session"]


def test_a_client_supplied_subsystem_never_reaches_the_audit_row():
    """``channels`` is written straight into a ``text[]`` column whose
    endpoint caps it at 8 entries of 32 characters. The gateway records a
    MAPPED value from a two-word vocabulary, never the client's own string,
    so no client-controlled text reaches that column and the list can never
    grow past the cap however many channels are opened.

    ``ProxyProcess.subsystem_requested`` already refuses everything but
    sftp, so the long string below cannot arrive in production -- recording
    a mapped value rather than an echoed one is what makes that a defence in
    depth instead of the only defence."""
    server = _authenticated(GatewaySSHServer(_context(limiter=_limiter()), CLIENT_IP))

    server._note_channel_type(FakeProcess(subsystem="x" * 200))
    server._note_channel_type(FakeProcess(subsystem="sftp"))
    server._note_channel_type(FakeProcess())

    assert server.channel_types == [mod.SESSION_CHANNEL_TYPE, mod.SFTP_CHANNEL_TYPE]
