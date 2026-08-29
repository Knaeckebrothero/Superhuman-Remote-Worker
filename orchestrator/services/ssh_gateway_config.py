"""Configuration and cryptographic policy for the SSH gateway.

Every algorithm list here is pinned rather than inherited. asyncssh's defaults
are meaningfully weaker than OpenSSH 10.2's: they include
diffie-hellman-group14-sha1, hmac-sha1, umac-64-etm@openssh.com, and register
``ssh-rsa`` with ``default=True``. Every string below was checked against the
installed asyncssh 2.24.0's own algorithm registries (``asyncssh.kex.get_kex_algs()``,
``asyncssh.mac.get_mac_algs()``, ``asyncssh.encryption.get_encryption_algs()``,
``asyncssh.public_key.get_public_key_algs()``) rather than assumed from the
spelling in a design doc — see tests/test_ssh_gateway_config.py's
`test_server_options_are_accepted_by_asyncssh`, which drives the exact
coroutine ``asyncssh.run_server`` uses to parse these kwargs
(``SSHServerConnectionOptions.construct``, confirmed by reading
``connection.py``'s ``run_server`` source), unmocked.

CORRECTION (fix round 2): pinning ``signature_algs`` does NOT stop a SHA-1
``ssh-rsa`` signature from authenticating -- an earlier version of this
docstring claimed otherwise, and that was wrong. Server-side,
``signature_algs`` is consulted only for ``_select_algs``'s config-name
validation and to build the advisory ``server-sig-algs`` extension sent to
the client (``connection.py:1992``); a client that ignores that hint
authenticates anyway, because ``SSHServerConnection.validate_public_key``
calls ``key.verify()`` directly (``connection.py:6217``) and
``SSHKey.verify()`` checks only the connecting KEY's own fixed
``all_sig_algorithms`` (``public_key.py:587``), never
``options.signature_algs``. Confirmed empirically: an RSA key accepts and
verifies a ``ssh-rsa``-signed blob even with SIGNATURE_ALGS excluding it
entirely (see `test_unnarrowed_rsa_key_accepts_sha1_as_the_negative_control`
in tests/test_ssh_gateway_config.py). Pinning ``signature_algs`` here is
still correct config hygiene, just not what excludes a SHA-1 RSA
*authentication* signature -- ``narrow_signature_algorithms()`` below,
applied to a specific connecting key, is what actually does that, and it is
inert until a caller invokes it (see its own docstring).

``SERVER_HOST_KEY_ALGS`` is the one list in this module that ``server_options``
below does NOT hand to asyncssh, and that is not an oversight. Read its own
comment before wiring it anywhere.
"""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass, field
from typing import Mapping, Optional

import asyncssh

from services.ssh_gateway_ca import load_user_ca

SIGNATURE_ALGS = (
    "ssh-ed25519",
    "sk-ssh-ed25519@openssh.com",
    "sk-ecdsa-sha2-nistp256@openssh.com",
    "rsa-sha2-512",
    "rsa-sha2-256",
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
)

KEX_ALGS = (
    "mlkem768x25519-sha256",
    "curve25519-sha256",
    "curve25519-sha256@libssh.org",
    "diffie-hellman-group16-sha512",
    "diffie-hellman-group18-sha512",
)

ENCRYPTION_ALGS = (
    "aes256-gcm@openssh.com",
    "aes128-gcm@openssh.com",
    "chacha20-poly1305@openssh.com",
)

MAC_ALGS = (
    "hmac-sha2-256-etm@openssh.com",
    "hmac-sha2-512-etm@openssh.com",
)

# Ed25519 only. RSA is deliberately not offered as a fallback -- see why
# below -- so there is exactly one entry.
#
# UNLIKE the four lists above, this one is never passed to asyncssh. Verified
# by reading connection.py's SSHServerConnectionOptions.prepare() signature
# (installed 2.24.0) end to end: it has no `server_host_key_algs` parameter at
# all. That name exists only on SSHClientConnectionOptions and the
# client-facing probe functions (`connect`, `get_server_host_key`,
# `get_server_auth_methods`), where it means "host-key algorithms the CLIENT
# will accept FROM a server" — the reverse of what a server-side policy needs.
# Confirmed empirically, not just by reading: calling
# ``SSHServerConnectionOptions.construct(None, config=(), server_host_key_algs=[...])``
# raises ``TypeError: prepare() got an unexpected keyword argument
# 'server_host_key_algs'``. See test_server_options_does_not_pass_server_host_key_algs.
#
# The name is not a typo or invention: it is real, working asyncssh API, and
# this exact codebase already uses it correctly on the CLIENT side —
# `canvas_ssh.py:359-369`'s `asyncssh.connect(..., known_hosts=EMPTY_KNOWN_HOSTS,
# server_host_key_algs=["ssh-ed25519"])`, matching the spec's §3.3, pins which
# of a *workspace's* host key types the gateway will accept when it dials out
# as a client. That is a different connection, a different options class, and
# a different list (one workspace host key to disambiguate from its own RSA
# key) than this constant, which is the gateway's OWN inbound identity as
# seen by connecting users. The likely origin of this task's brief passing it
# into `server_options()` is conflating those two uses of the same name.
#
# WHY THIS LIST HAS ONE ENTRY, NOT AN ED25519+RSA-FALLBACK PAIR: a server's
# host-key algorithm set is derived entirely from the *type* of key material
# handed to `server_host_keys`, with no per-key filter available anywhere in
# asyncssh's options layer — an Ed25519 key contributes exactly
# `ssh-ed25519`, but an RSA key contributes its FULL `RSAKey.sig_algorithms`
# tuple: `rsa-sha2-256`, `rsa-sha2-512`, four `ssh-rsa-shaNNN@ssh.com`
# variants, AND legacy SHA-1 `ssh-rsa` — verified empirically against a real
# generated RSA host key
# (tests/test_ssh_gateway_config.py::test_load_config_rejects_a_non_ed25519_host_key).
# `signature_algs` does NOT filter this; it governs client authentication
# signatures only (confirmed by reading `SSHServerConnection.__init__`, where
# `self._server_host_key_algs = list(options.server_host_keys.keys())` is
# built straight from loaded key material with no reference to
# `options.signature_algs`). Since nothing below this layer can keep an RSA
# host key from re-admitting SHA-1 `ssh-rsa` once loaded, this policy is
# enforced one layer up instead: `load_config`'s `_require_ed25519_host_key`
# opens and parses every configured host key and refuses anything whose
# algorithm is not `ssh-ed25519`, before any key ever reaches
# `server_options()`/`run_server`. That is why RSA is not offered as a
# fallback host key at all -- there is no lever anywhere in the options
# layer to keep only its SHA-2 variants for a host key, unlike a specific
# *connecting user's* authentication key, where `narrow_signature_
# algorithms()` (below) can narrow that one key instance to keep
# `rsa-sha2-256`/`rsa-sha2-512` while dropping `ssh-rsa` -- a per-connection
# operation with no host-key equivalent, since the server's host-key set is
# fixed for the life of the process, not chosen fresh per connection.
# CORRECTION (fix round 2): an earlier version of this comment claimed
# `signature_algs` itself is what lets RSA survive on the auth side. It does
# not enforce that or anything else against an inbound signature -- see the
# module docstring's correction and `narrow_signature_algorithms`'s
# docstring for what actually does. OpenSSH has supported Ed25519 host keys
# since 6.5 (2014) and JetBrains's own SSH stack does too, so dropping RSA
# here excludes no client this gateway must serve in 2026.
SERVER_HOST_KEY_ALGS = ("ssh-ed25519",)

_SIGNATURE_ALGS_BYTES = frozenset(alg.encode("ascii") for alg in SIGNATURE_ALGS)


def narrow_signature_algorithms(key: asyncssh.SSHKey) -> None:
    """Narrow ``key``'s own signature algorithms to intersect with SIGNATURE_ALGS.

    INERT UNTIL A CALLER INVOKES IT -- nothing in this codebase calls this
    yet. ``signature_algs`` (the config kwarg server_options() hands to
    ``asyncssh.run_server``) does NOT gate which signature algorithm
    asyncssh accepts when verifying an inbound client authentication
    signature; see the module docstring's fix-round-2 correction for the
    full citation trail. In short: ``SSHServerConnection.validate_public_key``
    (``connection.py:6199``) calls ``key.verify()`` directly
    (``connection.py:6217``), and ``SSHKey.verify()`` (``public_key.py:580``)
    checks only ``sig_algorithm in self.all_sig_algorithms`` -- the KEY's
    own fixed, per-type set, never ``options.signature_algs``. For RSA,
    ``all_sig_algorithms`` always includes legacy SHA-1 ``ssh-rsa``
    (``rsa.py:98-107``), so a plain RSA key accepts and verifies a
    ``ssh-rsa``-signed authentication blob no matter what SIGNATURE_ALGS
    says -- confirmed empirically, see
    ``test_unnarrowed_rsa_key_accepts_sha1_as_the_negative_control``.

    The only way to actually refuse a SHA-1 RSA signature is to narrow the
    KEY OBJECT itself, per connection, before it is used to verify
    anything -- which is what this function does: it mutates
    ``sig_algorithms``/``all_sig_algorithms`` on the given instance,
    shadowing the class attribute so every other instance of the same key
    type (and the class itself) is unaffected. Task 6's connecting-key
    resolution (wherever this plan's gateway resolves a client's key before
    ``validate_public_key`` verifies it) is the intended call site -- that
    wiring is carried forward, not built here.

    RISK, made loud rather than silent: this relies on
    ``all_sig_algorithms``/``sig_algorithms`` being plain, writable instance
    attributes, which is true in the installed 2.24.0 but is not a
    documented contract. If a future asyncssh makes them read-only (or
    otherwise silently drops the assignment), this raises ``AssertionError``
    immediately rather than letting a caller believe SHA-1 was excluded
    when it was not -- see
    ``test_narrow_signature_algorithms_fails_loudly_if_narrowing_has_no_effect``.
    """
    narrowed_sig_algorithms = tuple(
        alg for alg in key.sig_algorithms if alg in _SIGNATURE_ALGS_BYTES
    )
    narrowed_all_sig_algorithms = set(key.all_sig_algorithms) & _SIGNATURE_ALGS_BYTES

    key.sig_algorithms = narrowed_sig_algorithms
    key.all_sig_algorithms = narrowed_all_sig_algorithms

    if key.all_sig_algorithms != narrowed_all_sig_algorithms:
        raise AssertionError(
            "narrow_signature_algorithms: assigning all_sig_algorithms had "
            "no effect -- this asyncssh's SSHKey no longer exposes it as a "
            "plain writable attribute, so this key was NOT narrowed and "
            "still accepts every algorithm its type supports"
        )


@dataclass(frozen=True)
class GatewayConfig:
    host_key_paths: tuple[str, ...]
    user_ca_path: str
    orchestrator_url: str
    # repr=False: this is the orchestrator's internal API key. A frozen
    # dataclass's generated __repr__ prints every field verbatim by default,
    # which would otherwise put the raw key into any log line, exception
    # message, or debugger inspection that reprs a GatewayConfig — the same
    # class of leak Task 1's review checked for on the CA key. repr=False
    # omits the field from __repr__ entirely (and therefore from __str__,
    # which falls back to __repr__ here), rather than substituting a
    # placeholder, so there is nothing partial to accidentally match against.
    # It protects repr()/str() ONLY: dataclasses.asdict(config) walks
    # dataclasses.fields() directly and ignores the repr metadata entirely,
    # so it still returns internal_key verbatim (confirmed empirically) --
    # do not asdict() a GatewayConfig anywhere that result might be logged
    # or serialized.
    internal_key: str = field(repr=False)
    allowed_origins: tuple[str, ...]
    login_timeout: int = 20
    keepalive_interval: int = 30
    # The gateway's own outbound call to the orchestrator's internal
    # ssh-targets API (services.ssh_gateway_client.resolve_target), not to
    # be confused with login_timeout above (asyncssh's inbound pre-auth
    # budget). Was a bare `timeout=10.0` literal in resolve_target until a
    # review flagged it as the one gateway budget with no config lever --
    # every sibling budget here is a validated GatewayConfig field.
    orchestrator_request_timeout: float = 10.0
    max_preauth_connections: int = 64
    preauth_rate_per_minute: int = 60
    max_channels_per_connection: int = 12
    max_attachments_per_workspace: int = 4
    require_wss_token: bool = True
    # HMAC key for the token a USER presents to the WSS front door. Emphatically
    # NOT ``internal_key``: that one is the platform's service-to-service
    # credential for ~50 ``require_internal`` endpoints, and the plan's original
    # design handed it to every SSH user's laptop (ruling G38 --
    # services/ssh_gateway_token.py's docstring has the full account). Shares
    # ``repr=False`` with ``internal_key`` for the same reason, and shares the
    # value with the orchestrator through one Kubernetes Secret, which is what
    # lets a token minted by any orchestrator replica verify at the gateway with
    # no shared state.
    session_jwt_secret: str = field(default="", repr=False)
    # Source addresses whose ``X-Forwarded-For`` header the gateway believes.
    # Without a known ingress hop the header is client-supplied fiction, and
    # trusting it (as the plan's original ``_client_ip`` did, taking its FIRST
    # entry) lets any client mint a fresh source IP per connection and nullify
    # per-source rate limiting entirely.
    #
    # The dataclass default is empty and means "trust the socket peer", but
    # ``load_config`` will NOT reach it by omission -- an unset environment
    # variable is a boot failure, and empty is reachable only through the
    # explicit ``SSH_GATEWAY_TRUSTED_PROXIES=none``. See load_config for why
    # neither default is safe to inherit silently.
    trusted_proxies: tuple[str, ...] = ()
    # The raw TCP SSH listener. Task 11 ships ``containerPort: 2222`` and an
    # optional LoadBalancer on it, and every step of Task 12's live gate is an
    # ``ssh -p 2222`` -- so this is the port those front. 0 asks the OS for an
    # ephemeral port, which is how tests bind without colliding; a deployment
    # that sets 0 gets a random port and the startup log line is the only place
    # it appears.
    ssh_listen_host: str = "0.0.0.0"
    ssh_listen_port: int = 2222

    def __post_init__(self) -> None:
        """Reject a non-positive cap rather than constructing it silently.

        Before this, login_timeout=-5 or max_channels_per_connection=0
        constructed a GatewayConfig without complaint, and login_timeout
        flows straight into asyncssh's run_server -- a degenerate value
        there is a footgun (0 or negative channel/connection caps would
        mean "never admit anything" at best and something asyncssh itself
        does not validate at worst) discovered at runtime instead of at
        construction.
        """
        for field_name in (
            "login_timeout",
            "keepalive_interval",
            "orchestrator_request_timeout",
            "max_preauth_connections",
            "preauth_rate_per_minute",
            "max_channels_per_connection",
            "max_attachments_per_workspace",
        ):
            value = getattr(self, field_name)
            if value <= 0:
                raise ValueError(
                    f"GatewayConfig.{field_name} must be positive, got {value}"
                )
        # Not in the loop above: 0 is legal here and means "ask the OS for an
        # ephemeral port" (see the field's comment), so the check is a range,
        # not a positivity test.
        if not 0 <= self.ssh_listen_port <= 65535:
            raise ValueError(
                "GatewayConfig.ssh_listen_port must be 0..65535, got "
                f"{self.ssh_listen_port}"
            )


def _require_ed25519_host_key(path: str) -> None:
    """Reject any host key that is not Ed25519.

    SERVER_HOST_KEY_ALGS says ``("ssh-ed25519",)``, but there is no asyncssh
    server-side option that enforces it -- see that constant's own comment.
    A server's advertised host-key algorithms come straight from the *type*
    of key material loaded via `server_host_keys`, with no per-key filter
    available anywhere in the options layer, so this function is the ONLY
    place this policy can actually be enforced. It must run before any key
    reaches `server_options()`/`run_server`, matching the shape of Task 1's
    `SshUserCa.__init__`, the CA key's own one chokepoint: load the key,
    reject anything unusable, then reject the wrong algorithm by name.

    OpenSSH has supported Ed25519 host keys since 6.5 (2014) and JetBrains's
    own SSH stack does too, so this is not a compatibility compromise.
    """
    try:
        # Binary, not text: asyncssh.import_private_key accepts bytes or
        # str (BytesOrStr), and a valid PKCS#1/PKCS#8 DER-encoded key is
        # not valid UTF-8. Opening in text mode would reject such a key
        # with a message blaming the key's content rather than the mode
        # this file read it in.
        with open(path, "rb") as handle:
            key = asyncssh.import_private_key(handle.read())
    except Exception as exc:
        raise ValueError(
            f"ssh-gateway host key {path!r} is not a usable private key"
        ) from exc

    algorithm = key.get_algorithm()
    if algorithm != "ssh-ed25519":
        raise ValueError(
            f"ssh-gateway host key {path!r} must be Ed25519 (ssh-ed25519), "
            f"got {algorithm!r}"
        )


def load_config(env: Optional[Mapping[str, str]] = None) -> GatewayConfig:
    """Build config from the environment, failing closed on anything missing.

    A gateway that starts with a broken config and fails at the first
    connection is worse than one that refuses to start: the former looks
    healthy (process up, port open) until a real user hits it. Every value
    the gateway cannot function without is therefore validated here, at
    startup, rather than left to surface downstream.
    """
    src = env if env is not None else os.environ

    host_keys = tuple(
        p.strip()
        for p in (src.get("SSH_GATEWAY_HOST_KEYS") or "").split(",")
        if p.strip()
    )
    if not host_keys:
        raise ValueError(
            "ssh-gateway requires at least one host key (SSH_GATEWAY_HOST_KEYS)"
        )
    for path in host_keys:
        _require_ed25519_host_key(path)

    user_ca_path = (src.get("SSH_GATEWAY_USER_CA") or "").strip()
    if not user_ca_path:
        raise ValueError(
            "ssh-gateway requires the user CA path (SSH_GATEWAY_USER_CA); "
            "without it no inner-hop certificate can ever be minted, so every "
            "connection would authenticate and then fail"
        )
    # Parse it now, the same way _require_ed25519_host_key parses host keys:
    # a missing file, a .pub, an encrypted key, or a non-Ed25519 CA must fail
    # here, not at the first attempted mint. Task 1's SshUserCa.__init__
    # already refuses anything but Ed25519 (and anything unparseable) --
    # reuse it rather than re-deriving that check, and let it raise: a
    # missing file surfaces as OSError, a wrong key type or unusable content
    # as ValueError, whichever asyncssh/SshUserCa itself produces.
    load_user_ca(user_ca_path)

    internal_key = (src.get("MCP_INTERNAL_KEY") or "").strip()
    if not internal_key:
        raise ValueError("ssh-gateway requires the orchestrator internal key")

    origins = tuple(
        o.strip()
        for o in (src.get("SSH_GATEWAY_ALLOWED_ORIGINS") or "").split(",")
        if o.strip()
    )
    if not origins:
        raise ValueError(
            "ssh-gateway requires an explicit origin allow-list; an empty list "
            "would accept cross-site WebSocket handshakes"
        )

    require_wss_token = src.get("SSH_GATEWAY_REQUIRE_TOKEN", "true").lower() != "false"

    # Deliberately NOT .strip()ed, unlike every other value read here. The
    # orchestrator that mints these tokens reads the same variable with a bare
    # ``os.environ.get("SESSION_JWT_SECRET", "")`` (main.py:1417), so stripping
    # on this side alone would key two different HMACs from one Secret the
    # moment its value carries a trailing newline -- which is exactly what a
    # YAML block scalar or a hand-rolled Secret produces. Every token would
    # then be refused, with nothing in either log to say why. The emptiness
    # test below still strips, because a whitespace-only secret is not a
    # secret.
    session_jwt_secret = src.get("SESSION_JWT_SECRET") or ""
    if require_wss_token and not session_jwt_secret.strip():
        raise ValueError(
            "ssh-gateway requires SESSION_JWT_SECRET to verify the WSS attach "
            "token minted by the orchestrator; set it, or set "
            "SSH_GATEWAY_REQUIRE_TOKEN=false to run the WSS transport with no "
            "user credential at all"
        )

    # No default, in EITHER direction: this is one of the values where both
    # possible defaults are wrong, so the operator has to say which one they
    # mean. Omitting it used to mean "trust nothing", which reads safe and is
    # not: behind an ingress, every WSS client is then bucketed under the
    # ingress pod's own IP, and because refused handshakes share a source's
    # rate window (see GatewayLimiter.note_handshake_refusal), one attacker's
    # flood burns the whole fleet's admission budget for a minute. Defaulting
    # the other way -- trusting any hop -- is the spoofable header this
    # setting exists to prevent. The empty origin allow-list above fails
    # closed for the same shape of reason.
    raw_trusted = src.get("SSH_GATEWAY_TRUSTED_PROXIES")
    if raw_trusted is None or not raw_trusted.replace(",", "").strip():
        raise ValueError(
            "ssh-gateway requires SSH_GATEWAY_TRUSTED_PROXIES: the source "
            "addresses whose X-Forwarded-For header it may believe (the "
            "ingress hop, as an IP or CIDR list). Set it to "
            "SSH_GATEWAY_TRUSTED_PROXIES=none if nothing proxies this "
            "gateway, which trusts the socket peer instead -- but say so "
            "explicitly: left unset behind an ingress, every client is rate "
            "limited as one source and a single flood locks out the fleet"
        )

    if raw_trusted.strip().lower() == "none":
        # The explicit opt-out, and it must be the WHOLE value: "10.0.0.0/8,
        # none" is a typo, not a hop plus an opt-out, and falls through to the
        # CIDR validation below, which rejects it by name.
        trusted_proxies: tuple[str, ...] = ()
    else:
        trusted_proxies = tuple(p.strip() for p in raw_trusted.split(",") if p.strip())
    for entry in trusted_proxies:
        try:
            ipaddress.ip_network(entry, strict=False)
        except ValueError as exc:
            # A typo must not degrade silently. Left unvalidated, it would
            # never match, every request would fall back to the ingress pod's
            # own IP, and the whole fleet would share one rate-limit bucket.
            raise ValueError(
                f"SSH_GATEWAY_TRUSTED_PROXIES entry {entry!r} is not an IP "
                "address or CIDR"
            ) from exc

    raw_port = (src.get("SSH_GATEWAY_SSH_PORT") or "").strip()
    if raw_port:
        try:
            ssh_listen_port = int(raw_port)
        except ValueError as exc:
            raise ValueError(
                f"SSH_GATEWAY_SSH_PORT must be an integer, got {raw_port!r}"
            ) from exc
        if not 0 <= ssh_listen_port <= 65535:
            raise ValueError(
                f"SSH_GATEWAY_SSH_PORT must be 0..65535, got {ssh_listen_port}"
            )
    else:
        ssh_listen_port = 2222

    return GatewayConfig(
        host_key_paths=host_keys,
        user_ca_path=user_ca_path,
        orchestrator_url=(
            src.get("ORCHESTRATOR_URL") or "http://orchestrator:8085"
        ).rstrip("/"),
        internal_key=internal_key,
        allowed_origins=origins,
        require_wss_token=require_wss_token,
        session_jwt_secret=session_jwt_secret,
        trusted_proxies=trusted_proxies,
        ssh_listen_host=(src.get("SSH_GATEWAY_SSH_HOST") or "0.0.0.0").strip(),
        ssh_listen_port=ssh_listen_port,
    )


def server_options(config: GatewayConfig) -> dict:
    """Exact kwargs for ``asyncssh.run_server``.

    Every entry that looks redundant is disabling an asyncssh default that is
    wrong for a multi-tenant internet-facing gateway. There is no
    `server_host_key_algs` entry here — see SERVER_HOST_KEY_ALGS's comment
    above for why passing one would crash the first connection.
    """
    return {
        "server_host_keys": list(config.host_key_paths),
        "signature_algs": SIGNATURE_ALGS,
        "kex_algs": KEX_ALGS,
        "encryption_algs": ENCRYPTION_ALGS,
        "mac_algs": MAC_ALGS,
        "compression_algs": ["none"],
        # Default 'utf-8' corrupts or raises on binary session data.
        "encoding": None,
        # Default True. §3.4 refuses agent forwarding; it does not happen for free.
        "agent_forwarding": False,
        "x11_forwarding": False,
        "line_editor": False,
        "gss_host": None,
        # Only public-key auth is meant to reach this gateway. These three
        # are False today only because the not-yet-written SSHServer
        # subclass inherits validate_password/validate_kbdint_response/
        # validate_host_based_user_key, which the base class returns
        # False/None from by default -- an accident of what that subclass
        # happens not to override, not a policy this file states. Pinning
        # them here means a later subclass adding one of those methods for
        # an unrelated reason cannot silently enable password/kbdint/
        # host-based auth on an internet-facing listener that has no
        # MaxAuthTries (asyncssh ships no auth-attempt limiter at all).
        "password_auth": False,
        "kbdint_auth": False,
        "host_based_auth": False,
        # Default 120s; every pre-auth connection holds a socket for that long.
        "login_timeout": config.login_timeout,
        # Default 0 (disabled).
        "keepalive_interval": config.keepalive_interval,
    }
