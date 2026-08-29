import subprocess

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

BASE_ENV = {
    "SSH_GATEWAY_HOST_KEYS": "/run/secrets/gw/ed25519,/run/secrets/gw/rsa",
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
    Same weak-algorithm policy applies to it as to the other four."""
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


@pytest.fixture
def host_key_paths(tmp_path):
    ed_path = tmp_path / "ed25519"
    rsa_path = tmp_path / "rsa"
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(ed_path)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["ssh-keygen", "-t", "rsa", "-b", "3072", "-N", "", "-f", str(rsa_path)],
        check=True,
        capture_output=True,
    )
    return str(ed_path), str(rsa_path)


@pytest.mark.asyncio
async def test_server_options_are_accepted_by_asyncssh(host_key_paths):
    """The real proof, not a dict-shape proxy for it: feed server_options()'s
    dict through the exact option-parsing coroutine asyncssh.run_server calls
    internally (`SSHServerConnectionOptions.construct`, confirmed by reading
    run_server's source at connection.py:9128), unmocked, against real host
    key files. A key-name typo or an argument run_server does not accept
    raises here; asserting the dict merely has certain keys would not catch
    either."""
    ed_path, rsa_path = host_key_paths
    env = {**BASE_ENV, "SSH_GATEWAY_HOST_KEYS": f"{ed_path},{rsa_path}"}
    opts = server_options(load_config(env))

    constructed = await asyncssh.SSHServerConnectionOptions.construct(
        None, config=(), **opts
    )

    assert constructed.kex_algs
