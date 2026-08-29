"""Configuration and cryptographic policy for the SSH gateway.

Every algorithm list here is pinned rather than inherited. asyncssh's defaults
are meaningfully weaker than OpenSSH 10.2's: they include
diffie-hellman-group14-sha1, hmac-sha1, umac-64-etm@openssh.com, and — most
importantly — register ``ssh-rsa`` with ``default=True``, so a gateway that does
not pin ``signature_algs`` accepts SHA-1 RSA signatures that OpenSSH disabled in
8.8 (2021). Every string below was checked against the installed asyncssh
2.24.0's own algorithm registries (``asyncssh.kex.get_kex_algs()``,
``asyncssh.mac.get_mac_algs()``, ``asyncssh.encryption.get_encryption_algs()``,
``asyncssh.public_key.get_public_key_algs()``) rather than assumed from the
spelling in a design doc — see tests/test_ssh_gateway_config.py's
`test_server_options_are_accepted_by_asyncssh`, which drives the exact
coroutine ``asyncssh.run_server`` uses to parse these kwargs
(``SSHServerConnectionOptions.construct``, confirmed by reading
``connection.py``'s ``run_server`` source), unmocked.

``SERVER_HOST_KEY_ALGS`` is the one list in this module that ``server_options``
below does NOT hand to asyncssh, and that is not an oversight. Read its own
comment before wiring it anywhere.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Mapping, Optional

import asyncssh

SIGNATURE_ALGS = [
    "ssh-ed25519",
    "sk-ssh-ed25519@openssh.com",
    "sk-ecdsa-sha2-nistp256@openssh.com",
    "rsa-sha2-512",
    "rsa-sha2-256",
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
]

KEX_ALGS = [
    "mlkem768x25519-sha256",
    "curve25519-sha256",
    "curve25519-sha256@libssh.org",
    "diffie-hellman-group16-sha512",
    "diffie-hellman-group18-sha512",
]

ENCRYPTION_ALGS = [
    "aes256-gcm@openssh.com",
    "aes128-gcm@openssh.com",
    "chacha20-poly1305@openssh.com",
]

MAC_ALGS = [
    "hmac-sha2-256-etm@openssh.com",
    "hmac-sha2-512-etm@openssh.com",
]

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
# fallback host key at all, unlike SIGNATURE_ALGS/SERVER host-key
# *client-auth* algorithms further up this module, where RSA survives as
# `rsa-sha2-256`/`rsa-sha2-512` because `signature_algs` genuinely is
# enforceable by asyncssh. OpenSSH has supported Ed25519 host keys since 6.5
# (2014) and JetBrains's own SSH stack does too, so this excludes no client
# this gateway must serve in 2026.
SERVER_HOST_KEY_ALGS = ["ssh-ed25519"]


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
    internal_key: str = field(repr=False)
    allowed_origins: tuple[str, ...]
    login_timeout: int = 20
    keepalive_interval: int = 30
    max_preauth_connections: int = 64
    preauth_rate_per_minute: int = 60
    max_channels_per_connection: int = 12
    max_attachments_per_workspace: int = 4
    require_wss_token: bool = True


def _require_ed25519_host_key(path: str) -> None:
    """Reject any host key that is not Ed25519.

    SERVER_HOST_KEY_ALGS says ``["ssh-ed25519"]``, but there is no asyncssh
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
        with open(path, encoding="utf-8") as handle:
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

    return GatewayConfig(
        host_key_paths=host_keys,
        user_ca_path=user_ca_path,
        orchestrator_url=(
            src.get("ORCHESTRATOR_URL") or "http://orchestrator:8085"
        ).rstrip("/"),
        internal_key=internal_key,
        allowed_origins=origins,
        require_wss_token=(
            src.get("SSH_GATEWAY_REQUIRE_TOKEN", "true").lower() != "false"
        ),
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
        # Default 120s; every pre-auth connection holds a socket for that long.
        "login_timeout": config.login_timeout,
        # Default 0 (disabled).
        "keepalive_interval": config.keepalive_interval,
    }
