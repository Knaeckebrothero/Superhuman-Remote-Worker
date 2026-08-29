import subprocess

import asyncssh
import pytest

from services.ssh_gateway_config import (
    ENCRYPTION_ALGS,
    KEX_ALGS,
    MAC_ALGS,
    SERVER_HOST_KEY_ALGS,
    SIGNATURE_ALGS,
    GatewayConfig,
    load_config,
    narrow_signature_algorithms,
    server_options,
)

# Placeholder paths -- both are overwritten with real, valid Ed25519 keys by
# the autouse `_real_keys_for_base_env` fixture below, before any test in
# this file executes. load_config now opens and parses both the host key(s)
# (_require_ed25519_host_key) and the CA key (load_user_ca, via Task 1's
# SshUserCa), so a fake path here would fail every test that doesn't
# specifically want to exercise that failure.
BASE_ENV = {
    "SSH_GATEWAY_HOST_KEYS": "",
    "SSH_GATEWAY_USER_CA": "",
    "ORCHESTRATOR_URL": "http://orchestrator:8085",
    "MCP_INTERNAL_KEY": "internal",
    "SSH_GATEWAY_ALLOWED_ORIGINS": "https://cockpit.srw.works",
    # Required since Task 8: the WSS bearer token is an HMAC under
    # SESSION_JWT_SECRET, so load_config refuses to start without it unless
    # SSH_GATEWAY_REQUIRE_TOKEN is explicitly "false" (ruling G38).
    "SESSION_JWT_SECRET": "test-only-session-secret",
}


@pytest.fixture(scope="session", autouse=True)
def _real_keys_for_base_env(tmp_path_factory):
    """Generate the real Ed25519 host key and CA key BASE_ENV needs, lazily.

    Fixtures only run when a test that depends on them is about to execute
    -- never during mere collection -- so a bare `--collect-only` does not
    mint a private key or leak a directory, unlike the module-level
    `subprocess.run(...)`/`tempfile.mkdtemp()` this file used before. Uses
    `tmp_path_factory` (session-scoped by nature) rather than the
    function-scoped `tmp_path`, and follows pytest's normal temp-dir
    retention/cleanup policy rather than a bare, uncleaned `mkdtemp()` --
    matching Task 1's sibling suite (`test_ssh_gateway_ca.py`), which
    generates its CA key through a `tmp_path`-based fixture rather than at
    import time.

    Session-scoped and autouse: runs once for the whole file, mutates
    BASE_ENV's dict in place before the first test body executes, so every
    other test in this file keeps referencing plain `BASE_ENV`/`{**BASE_ENV,
    ...}` with no signature changes needed.
    """
    key_dir = tmp_path_factory.mktemp("ssh_gateway_config")

    host_key = key_dir / "host_ed25519"
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(host_key)],
        check=True,
        capture_output=True,
    )

    ca_key = key_dir / "user_ca_ed25519"
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(ca_key)],
        check=True,
        capture_output=True,
    )

    BASE_ENV["SSH_GATEWAY_HOST_KEYS"] = str(host_key)
    BASE_ENV["SSH_GATEWAY_USER_CA"] = str(ca_key)


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
    with pytest.raises(ValueError, match="SSH_GATEWAY_HOST_KEYS"):
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
    RSA is not offered as a fallback at all (see
    test_load_config_rejects_a_non_ed25519_host_key for why), so the only
    algorithm this gateway's own inbound identity ever advertises is
    ssh-ed25519."""
    assert SERVER_HOST_KEY_ALGS == ("ssh-ed25519",)
    for name in SERVER_HOST_KEY_ALGS:
        assert "sha1" not in name.lower()
    assert "ssh-rsa" not in SERVER_HOST_KEY_ALGS


def test_requires_the_user_ca_path():
    """A gateway with no CA path can mint no inner-hop certificates at all --
    every connection would authenticate and then fail. Fail closed at startup
    instead of at the first connection."""
    with pytest.raises(ValueError, match="[Cc][Aa]"):
        load_config({**BASE_ENV, "SSH_GATEWAY_USER_CA": ""})


def test_load_config_rejects_a_non_ed25519_ca(tmp_path):
    """Task 1's SshUserCa.__init__ already refuses a non-Ed25519 CA key;
    load_config must actually call it (via load_user_ca) rather than just
    checking the path string is non-empty, or a wrong-type CA passes startup
    and fails at the first attempted mint -- verbatim the failure mode
    _require_ed25519_host_key exists to close for host keys, one field
    over."""
    rsa_ca_path = tmp_path / "rsa_ca"
    subprocess.run(
        ["ssh-keygen", "-t", "rsa", "-b", "3072", "-N", "", "-f", str(rsa_ca_path)],
        check=True,
        capture_output=True,
    )

    with pytest.raises(ValueError) as exc_info:
        load_config({**BASE_ENV, "SSH_GATEWAY_USER_CA": str(rsa_ca_path)})

    assert "ed25519" in str(exc_info.value).lower()


def test_load_config_rejects_a_missing_ca_file():
    """load_user_ca opens the file directly and load_config lets it raise
    (no wrapping) -- a missing CA file surfaces as OSError, not ValueError,
    which is still fail-closed at startup rather than at first connection."""
    with pytest.raises(OSError):
        load_config({**BASE_ENV, "SSH_GATEWAY_USER_CA": "/nonexistent/path/gone"})


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
    describe: that the config this module accepts causes the real, installed
    asyncssh to advertise exactly {ssh-ed25519} as its host-key algorithm
    during KEX, GIVEN NO `<path>-cert.pub` SIDECAR NEXT TO THE HOST KEY.
    A sidecar there is auto-paired by asyncssh's key loading and would add
    `ssh-ed25519-cert-v01@openssh.com` too (confirmed empirically) -- not a
    weak algorithm, so not a defect, just outside what this fixture creates
    and worth being precise about rather than claiming "the only thing ever
    advertised."
    """
    opts = server_options(load_config(BASE_ENV))

    constructed = await asyncssh.SSHServerConnectionOptions.construct(
        None, config=(), **opts
    )

    assert constructed.kex_algs
    assert set(constructed.server_host_keys.keys()) == {b"ssh-ed25519"}


def test_load_config_rejects_a_non_ed25519_host_key(tmp_path):
    """SERVER_HOST_KEY_ALGS says ("ssh-ed25519",), but asyncssh has no
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


def test_algorithm_policy_lists_are_immutable():
    """A caller mutating the list server_options() returns must not corrupt
    the module's own policy constants for every other connection -- tuples
    close this by construction rather than by convention."""
    assert isinstance(SIGNATURE_ALGS, tuple)
    assert isinstance(KEX_ALGS, tuple)
    assert isinstance(MAC_ALGS, tuple)
    assert isinstance(ENCRYPTION_ALGS, tuple)
    assert isinstance(SERVER_HOST_KEY_ALGS, tuple)

    opts = server_options(load_config(BASE_ENV))
    with pytest.raises(AttributeError):
        opts["signature_algs"].append("evil")


def test_server_options_pins_password_kbdint_and_host_based_auth_off():
    """Only public-key auth is meant to reach this gateway. These three are
    False today only because the not-yet-written SSHServer subclass's
    inherited validate_password/validate_kbdint_response/
    validate_host_based_user_key return False/None by default -- pinning
    them here means a later subclass adding one of those methods for an
    unrelated reason cannot silently open password/kbdint/host-based auth on
    a listener with no MaxAuthTries."""
    opts = server_options(load_config(BASE_ENV))
    assert opts["password_auth"] is False
    assert opts["kbdint_auth"] is False
    assert opts["host_based_auth"] is False


def _gateway_config_kwargs(**overrides):
    base = dict(
        host_key_paths=("/x",),
        user_ca_path="/y",
        orchestrator_url="http://z",
        internal_key="k",
        allowed_origins=("https://a",),
    )
    base.update(overrides)
    return base


def test_gateway_config_rejects_non_positive_caps():
    """login_timeout=-5, max_channels_per_connection=0,
    preauth_rate_per_minute=-1 all constructed a GatewayConfig without
    complaint before this fix, and login_timeout flows straight into
    asyncssh's run_server."""
    for field_name, bad_value in (
        ("login_timeout", -5),
        ("login_timeout", 0),
        ("keepalive_interval", -1),
        ("orchestrator_request_timeout", -1),
        ("orchestrator_request_timeout", 0),
        ("max_preauth_connections", 0),
        ("preauth_rate_per_minute", -1),
        ("max_channels_per_connection", 0),
        ("max_attachments_per_workspace", 0),
    ):
        with pytest.raises(ValueError, match=field_name):
            GatewayConfig(**_gateway_config_kwargs(**{field_name: bad_value}))


def test_gateway_config_accepts_the_positive_defaults():
    """Negative control for the above: construction must still succeed with
    ordinary positive values (the dataclass defaults)."""
    config = GatewayConfig(**_gateway_config_kwargs())
    assert config.login_timeout == 20
    assert config.max_channels_per_connection == 12
    assert config.orchestrator_request_timeout == 10.0


def _sign_and_verify(key, pub, algorithm: bytes, data: bytes) -> bool:
    return pub.verify(data, key.sign(data, algorithm))


def test_narrow_signature_algorithms_rejects_sha1_rsa_but_keeps_sha2():
    """signature_algs never reaches SSHKey.verify() server-side (see
    narrow_signature_algorithms's own docstring for the full citation
    trail); the only way to actually refuse a SHA-1 RSA signature is to
    narrow the key object itself. Both directions in one test: narrowing
    must reject what it's supposed to reject AND keep what it's supposed to
    keep, or a helper that narrows too aggressively would break every RSA
    key using SIGNATURE_ALGS's own rsa-sha2-256/512 entries."""
    key = asyncssh.generate_private_key("ssh-rsa", key_size=3072)
    pub = key.convert_to_public()
    data = b"session-id-and-auth-message"

    narrow_signature_algorithms(pub)

    assert _sign_and_verify(key, pub, b"ssh-rsa", data) is False
    assert _sign_and_verify(key, pub, b"rsa-sha2-256", data) is True


def test_unnarrowed_rsa_key_accepts_sha1_as_the_negative_control():
    """Without narrow_signature_algorithms, the exact vulnerability
    Important 1 identified: an RSA key accepts a SHA-1 ssh-rsa signature
    exactly as readily as rsa-sha2-256, with SIGNATURE_ALGS excluding
    ssh-rsa having no effect on this at all. This is what proves the test
    above is discriminating -- if narrow_signature_algorithms silently
    became a no-op, its rejection assertion would start matching this
    test's acceptance instead."""
    key = asyncssh.generate_private_key("ssh-rsa", key_size=3072)
    pub = key.convert_to_public()
    data = b"session-id-and-auth-message"

    assert _sign_and_verify(key, pub, b"ssh-rsa", data) is True
    assert _sign_and_verify(key, pub, b"rsa-sha2-256", data) is True


class _ReadOnlyAlgsKey:
    """A minimal stand-in for an SSHKey whose sig_algorithms/
    all_sig_algorithms cannot actually be changed -- simulating a future
    asyncssh where narrowing silently has no effect, to prove
    narrow_signature_algorithms notices rather than reporting success."""

    sig_algorithms = (b"ssh-rsa", b"rsa-sha2-256")
    all_sig_algorithms = frozenset({b"ssh-rsa", b"rsa-sha2-256"})

    def __setattr__(self, name, value):
        pass  # silently swallow -- exactly the failure mode being guarded against


def test_narrow_signature_algorithms_fails_loudly_if_narrowing_has_no_effect():
    """The main risk of mutating an instance attribute: if a future asyncssh
    makes all_sig_algorithms/sig_algorithms read-only, or otherwise silently
    swallows the assignment, narrow_signature_algorithms must not let that
    pass quietly -- an operator would otherwise believe SHA-1 was excluded
    when it was not."""
    with pytest.raises(AssertionError, match="had no effect"):
        narrow_signature_algorithms(_ReadOnlyAlgsKey())


# ---------------------------------------------------------------------------
# Task 8 additions: the user-presented attach token's key, the trusted ingress
# hop, and the TCP listener's bind address.
# ---------------------------------------------------------------------------


def test_requires_the_session_secret_when_the_wss_token_is_required():
    """Fail closed at boot, not at the first connection.

    The WSS bearer token is an HMAC under SESSION_JWT_SECRET (ruling G38 --
    it is emphatically NOT MCP_INTERNAL_KEY any more). Without the secret the
    gateway can verify nothing, so every attach would 4401 while the process
    looked healthy.
    """
    env = dict(BASE_ENV)
    env.pop("SESSION_JWT_SECRET", None)
    with pytest.raises(ValueError, match="SESSION_JWT_SECRET"):
        load_config(env)


def test_the_session_secret_is_optional_when_the_token_is_explicitly_disabled():
    env = dict(BASE_ENV)
    env.pop("SESSION_JWT_SECRET", None)
    env["SSH_GATEWAY_REQUIRE_TOKEN"] = "false"
    config = load_config(env)
    assert config.require_wss_token is False
    assert config.session_jwt_secret == ""


def test_the_session_secret_is_not_stripped():
    """The orchestrator reads SESSION_JWT_SECRET with a bare
    ``os.environ.get`` (main.py:1417) -- unstripped. If this side stripped
    it, a secret with a trailing newline (exactly what a YAML block scalar
    or a hand-made Secret produces) would key two different HMACs and every
    token minted by the orchestrator would be refused here, with no error
    anywhere to explain it."""
    config = load_config({**BASE_ENV, "SESSION_JWT_SECRET": "  padded \n"})
    assert config.session_jwt_secret == "  padded \n"


def test_session_secret_never_appears_in_repr():
    secret = "s3ss10n-do-not-leak-4b21"
    config = load_config({**BASE_ENV, "SESSION_JWT_SECRET": secret})
    assert secret not in repr(config)
    assert secret not in str(config)


def test_trusted_proxies_default_to_nothing():
    """No trusted hop means X-Forwarded-For is never believed -- the
    fail-closed default. Trusting it unconditionally (the plan's original
    _client_ip) let any client spoof a fresh source IP per connection and
    nullify per-source rate limiting entirely."""
    env = dict(BASE_ENV)
    env.pop("SSH_GATEWAY_TRUSTED_PROXIES", None)
    assert load_config(env).trusted_proxies == ()


def test_trusted_proxies_parse_as_cidrs():
    config = load_config(
        {**BASE_ENV, "SSH_GATEWAY_TRUSTED_PROXIES": "10.42.0.0/16, 192.168.1.7"}
    )
    assert config.trusted_proxies == ("10.42.0.0/16", "192.168.1.7")


def test_an_unparseable_trusted_proxy_fails_at_boot():
    """A typo'd CIDR must not silently become "trust nobody" (which quietly
    rate-limits every real client under one ingress IP) or "trust anybody"."""
    with pytest.raises(ValueError, match="SSH_GATEWAY_TRUSTED_PROXIES"):
        load_config({**BASE_ENV, "SSH_GATEWAY_TRUSTED_PROXIES": "10.42.0.0/33"})


def test_ssh_listener_defaults_to_2222():
    """Task 11 ships containerPort 2222 and Task 12's live gate is a series
    of `ssh -p 2222` commands; the default here is what makes those real."""
    env = dict(BASE_ENV)
    env.pop("SSH_GATEWAY_SSH_PORT", None)
    env.pop("SSH_GATEWAY_SSH_HOST", None)
    config = load_config(env)
    assert config.ssh_listen_port == 2222
    assert config.ssh_listen_host == "0.0.0.0"


def test_ssh_listener_port_is_overridable_and_range_checked():
    assert load_config(
        {**BASE_ENV, "SSH_GATEWAY_SSH_PORT": "2200"}
    ).ssh_listen_port == (2200)
    for bad in ("70000", "-1", "not-a-port"):
        with pytest.raises(ValueError, match="SSH_GATEWAY_SSH_PORT"):
            load_config({**BASE_ENV, "SSH_GATEWAY_SSH_PORT": bad})
