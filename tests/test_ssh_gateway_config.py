import subprocess
import tempfile
from pathlib import Path

import asyncssh
import pytest

from services.ssh_gateway_config import (
    KEX_ALGS,
    MAC_ALGS,
    SERVER_HOST_KEY_ALGS,
    SIGNATURE_ALGS,
    load_config,
    server_options,
)

# A real Ed25519 host key, generated once for the whole module (same
# module-level tempfile.mkdtemp() pattern tests/conftest.py already uses for
# WORKSPACE_PATH). load_config now opens and parses every configured host
# key to enforce SERVER_HOST_KEY_ALGS == ["ssh-ed25519"] (fix round 1 below),
# so BASE_ENV's host key must be a real, valid key on disk rather than the
# placeholder path this file used before that check existed.
_KEY_DIR = Path(tempfile.mkdtemp(prefix="ssh_gateway_config_test_"))
_ED25519_HOST_KEY = _KEY_DIR / "ed25519"
subprocess.run(
    ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(_ED25519_HOST_KEY)],
    check=True,
    capture_output=True,
)

BASE_ENV = {
    "SSH_GATEWAY_HOST_KEYS": str(_ED25519_HOST_KEY),
    "SSH_GATEWAY_USER_CA": "/run/secrets/gw/user-ca",
    "ORCHESTRATOR_URL": "http://orchestrator:8085",
    "MCP_INTERNAL_KEY": "internal",
    "SSH_GATEWAY_ALLOWED_ORIGINS": "https://cockpit.srw.works",
}


def test_no_sha1_anywhere():
    """asyncssh's defaults include diffie-hellman-group14-sha1 and hmac-sha1."""
    for name in KEX_ALGS + MAC_ALGS + SIGNATURE_ALGS:
        assert "sha1" not in name.lower()


def test_plain_ssh_rsa_is_not_accepted():
    """asyncssh registers ssh-rsa with default=True, so SHA-1 RSA signatures are
    accepted unless we pin the list. OpenSSH disabled these in 8.8."""
    assert "ssh-rsa" not in SIGNATURE_ALGS
    assert "rsa-sha2-512" in SIGNATURE_ALGS


def test_fido_types_are_accepted():
    assert "sk-ssh-ed25519@openssh.com" in SIGNATURE_ALGS


def test_requires_a_host_key():
    with pytest.raises(ValueError, match="host key"):
        load_config({**BASE_ENV, "SSH_GATEWAY_HOST_KEYS": ""})


def test_requires_the_internal_key():
    with pytest.raises(ValueError, match="internal key"):
        load_config({**BASE_ENV, "MCP_INTERNAL_KEY": ""})


def test_requires_an_allowed_origin():
    """An empty origin allow-list must fail closed, not allow everything."""
    with pytest.raises(ValueError, match="origin"):
        load_config({**BASE_ENV, "SSH_GATEWAY_ALLOWED_ORIGINS": ""})


def test_server_options_disable_the_dangerous_defaults():
    opts = server_options(load_config(BASE_ENV))
    assert opts["encoding"] is None  # default 'utf-8' corrupts binary
    assert opts["agent_forwarding"] is False  # asyncssh defaults True
    assert opts["x11_forwarding"] is False
    assert opts["line_editor"] is False
    assert opts["login_timeout"] <= 30  # default is 120
    assert opts["keepalive_interval"] > 0  # default 0 = disabled


def test_server_options_pin_every_algorithm_list():
    opts = server_options(load_config(BASE_ENV))
    for key in ("signature_algs", "kex_algs", "mac_algs", "encryption_algs"):
        assert opts[key], f"{key} must be pinned, not left at asyncssh defaults"


# ---------------------------------------------------------------------------
# Beyond the brief's baseline: closing gaps a dict-shape assertion cannot see.
# ---------------------------------------------------------------------------


def test_server_host_key_algs_have_no_sha1_or_plain_rsa():
    """SERVER_HOST_KEY_ALGS is the fifth pinned list; test_no_sha1_anywhere
    above does not cover it (it was never imported in the brief's own test).
    Fix round 1 makes this exact rather than just "no known-weak names":
    RSA is not offered as a fallback at all (see
    test_load_config_rejects_a_non_ed25519_host_key for why), so the only
    algorithm this gateway's own inbound identity ever advertises is
    ssh-ed25519."""
    assert SERVER_HOST_KEY_ALGS == ["ssh-ed25519"]
    for name in SERVER_HOST_KEY_ALGS:
        assert "sha1" not in name.lower()
    assert "ssh-rsa" not in SERVER_HOST_KEY_ALGS


def test_requires_the_user_ca_path():
    """A gateway with no CA path can mint no inner-hop certificates at all --
    every connection would authenticate and then fail. Fail closed at startup
    instead of at the first connection."""
    with pytest.raises(ValueError, match="[Cc][Aa]"):
        load_config({**BASE_ENV, "SSH_GATEWAY_USER_CA": ""})


def test_internal_key_never_appears_in_repr():
    """GatewayConfig is a frozen dataclass; the default __repr__ prints every
    field verbatim, which would put the internal API key into any log line
    or exception message that reprs the config."""
    secret = "s3cr3t-do-not-leak-9f3a"
    config = load_config({**BASE_ENV, "MCP_INTERNAL_KEY": secret})
    assert secret not in repr(config)
    assert secret not in str(config)


def test_orchestrator_url_defaults_when_absent():
    env = dict(BASE_ENV)
    del env["ORCHESTRATOR_URL"]
    assert load_config(env).orchestrator_url == "http://orchestrator:8085"


def test_orchestrator_url_strips_trailing_slash():
    env = {**BASE_ENV, "ORCHESTRATOR_URL": "http://orchestrator:8085/"}
    assert load_config(env).orchestrator_url == "http://orchestrator:8085"


def test_server_options_does_not_pass_server_host_key_algs():
    """asyncssh.SSHServerConnectionOptions has no `server_host_key_algs`
    parameter -- confirmed by reading the installed 2.24.0 source
    (connection.py's SSHServerConnectionOptions.prepare() signature) and
    empirically via SSHServerConnectionOptions.construct(). It exists ONLY on
    SSHClientConnectionOptions (and the client-facing probe functions
    connect/get_server_host_key/get_server_auth_methods), where it means
    "host-key algorithms the client will accept FROM a server" -- the
    opposite direction from what a server config needs. Including it in the
    kwargs handed to asyncssh.run_server raises TypeError on the very first
    connection: 'prepare() got an unexpected keyword argument
    server_host_key_algs'. See test_server_options_are_accepted_by_asyncssh
    below for the empirical proof."""
    opts = server_options(load_config(BASE_ENV))
    assert "server_host_key_algs" not in opts


@pytest.mark.asyncio
async def test_server_options_are_accepted_by_asyncssh():
    """The real proof, not a dict-shape proxy for it: feed server_options()'s
    dict through the exact option-parsing coroutine asyncssh.run_server calls
    internally (`SSHServerConnectionOptions.construct`, confirmed by reading
    run_server's source at connection.py:9128), unmocked, against a real host
    key file. A key-name typo or an argument run_server does not accept
    raises here; asserting the dict merely has certain keys would not catch
    either.

    The second assertion is the property SERVER_HOST_KEY_ALGS exists to
    describe, and the one that actually matters: not that the constant is
    the right list of strings, but that the config this module accepts
    causes the real, installed asyncssh to advertise exactly {ssh-ed25519}
    as its host-key algorithm during KEX. See
    test_load_config_rejects_a_non_ed25519_host_key for the negative control
    that shows this assertion (and the config it depends on) actually
    discriminates.
    """
    opts = server_options(load_config(BASE_ENV))

    constructed = await asyncssh.SSHServerConnectionOptions.construct(
        None, config=(), **opts
    )

    assert constructed.kex_algs
    assert set(constructed.server_host_keys.keys()) == {b"ssh-ed25519"}


def test_load_config_rejects_a_non_ed25519_host_key(tmp_path):
    """SERVER_HOST_KEY_ALGS says ["ssh-ed25519"], but asyncssh has no
    server-side option that can enforce it (see that constant's comment in
    ssh_gateway_config.py): a server's advertised host-key algorithms come
    straight from the *type* of key material loaded via `server_host_keys`,
    with no per-key filter available anywhere in the options layer. An RSA
    key's sig_algorithms includes legacy SHA-1 ssh-rsa plus four more
    @ssh.com variants regardless of what SIGNATURE_ALGS/KEX_ALGS/MAC_ALGS
    say -- reproduced directly below against a real generated RSA key. The
    only place left that can enforce Ed25519-only is here, at config load,
    before the key ever reaches server_options()/run_server.
    """
    rsa_path = tmp_path / "rsa"
    subprocess.run(
        ["ssh-keygen", "-t", "rsa", "-b", "3072", "-N", "", "-f", str(rsa_path)],
        check=True,
        capture_output=True,
    )

    with pytest.raises(ValueError) as exc_info:
        load_config({**BASE_ENV, "SSH_GATEWAY_HOST_KEYS": str(rsa_path)})

    message = str(exc_info.value)
    assert "ed25519" in message.lower()
    assert str(rsa_path) in message
