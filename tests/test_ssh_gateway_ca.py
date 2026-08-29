# tests/test_ssh_gateway_ca.py
import subprocess
import time

import asyncssh
import pytest
from asyncssh.public_key import CERT_TYPE_USER

from services.ssh_gateway_ca import (
    CERT_BACKDATE_SECONDS,
    DEFAULT_CERT_LIFETIME_SECONDS,
    MAX_CERT_LIFETIME_SECONDS,
    SshUserCa,
)


@pytest.fixture
def ca_pem(tmp_path):
    path = tmp_path / "ca"
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-C", "srw-user-ca", "-f", str(path)],
        check=True,
        capture_output=True,
    )
    return path.read_text()


def test_mint_produces_a_certificate_for_one_principal(ca_pem):
    _, cert = SshUserCa(ca_pem).mint("agent-host")
    assert cert.principals == ["agent-host"]


# asyncssh 2.24.0's SSHOpenSSHCertificate(V01) stores the validity window only
# as `_valid_after`/`_valid_before` (public_key.py:1572-1573) -- there is no
# public `valid_after`/`valid_before` property in this version. Confirmed by
# reading the installed source and grepping the whole package for any public
# accessor (none exists). The span test below is the one place that still
# reads them: a duration has no public accessor at all. "Is it valid right
# now" DOES have one (`cert.validate`), so that test uses it instead.


def test_certificate_is_short_lived(ca_pem):
    _, cert = SshUserCa(ca_pem).mint("agent-host")
    span = cert._valid_before - cert._valid_after  # private field; see note above
    # `now` is captured once in mint() and reused for both valid_after and
    # valid_before, so the span is exactly lifetime + backdate -- no
    # clock-skew slop needed, and none should be allowed: a silent multiple
    # of CERT_BACKDATE_SECONDS must fail this.
    assert span == DEFAULT_CERT_LIFETIME_SECONDS + CERT_BACKDATE_SECONDS


def test_certificate_is_valid_now(ca_pem):
    _, cert = SshUserCa(ca_pem).mint("agent-host")
    cert.validate(CERT_TYPE_USER, "agent-host")  # must not raise


def test_certificate_outside_its_window_is_rejected(ca_pem):
    """Drive the same rejection path asyncssh's own server auth uses.

    ``connection.py``'s user-certificate auth calls exactly
    ``cert.validate(CERT_TYPE_USER, cert_user)`` (verified at
    connection.py:6089). This exercises that real, public method rather than
    re-deriving expiry from the private validity fields.
    """
    _, cert = SshUserCa(ca_pem).mint("agent-host", lifetime_seconds=1)
    time.sleep(2)
    with pytest.raises(ValueError):
        cert.validate(CERT_TYPE_USER, "agent-host")


def test_mint_accepts_lifetime_at_the_ceiling(ca_pem):
    _, cert = SshUserCa(ca_pem).mint(
        "agent-host", lifetime_seconds=MAX_CERT_LIFETIME_SECONDS
    )
    span = cert._valid_before - cert._valid_after
    assert span == MAX_CERT_LIFETIME_SECONDS + CERT_BACKDATE_SECONDS


def test_mint_rejects_lifetime_beyond_the_ceiling(ca_pem):
    """Neither asyncssh nor OpenSSH bound valid_before; a typo feeding a huge
    lifetime_seconds through config would otherwise mint a years-long
    certificate from a module whose entire premise is short-livedness."""
    with pytest.raises(ValueError):
        SshUserCa(ca_pem).mint(
            "agent-host", lifetime_seconds=MAX_CERT_LIFETIME_SECONDS + 1
        )


def test_each_mint_uses_a_fresh_keypair(ca_pem):
    """A per-connection keypair means a leaked one is worthless in minutes and
    cannot be correlated across sessions.

    Compares ``.public_data``, not ``export_private_key()``: the OpenSSH
    private-key encoding embeds a random check-int, so two exports of the
    *same* key object are already unequal (empirically confirmed) -- that
    assertion would pass even if ``mint()`` returned one fixed key every
    time. ``.public_data`` is stable for a given key and differs only when
    the key itself differs. Also compares what each certificate actually
    signed (``cert.key``), not just what ``mint()`` handed back, so the
    check covers what got signed too.
    """
    ca = SshUserCa(ca_pem)
    first_key, first_cert = ca.mint("agent-host")
    second_key, second_cert = ca.mint("agent-host")
    assert first_key.public_data != second_key.public_data
    assert first_cert.key.public_data != second_cert.key.public_data


def test_mint_grants_only_pty_and_port_forwarding(ca_pem):
    """permit-pty is required for an interactive shell (the feature's primary
    use case) and permit-port-forwarding for direct-tcpip (JetBrains Gateway /
    ProxyJump), which the workspace's own `PermitOpen 127.0.0.1:*` /
    `AllowTcpForwarding local` narrow -- but only once the certificate allows
    forwarding at all; extensions are permissive-by-listing, so sshd_config
    cannot grant back what the certificate omits. asyncssh (like real
    `ssh-keygen -s`, confirmed via its `-O clear` option and its man page's
    "permitted by default" wording) defaults every permit-* to granted, so
    the three unneeded ones (X11-forwarding, agent-forwarding, user-rc) must
    be denied explicitly or they are inherited on. This asserts both
    directions in one equality: a missing permit-pty/permit-port-forwarding
    fails it, and so does any of the other three leaking back in."""
    _, cert = SshUserCa(ca_pem).mint("agent-host")
    assert cert.options == {"permit-pty": True, "permit-port-forwarding": True}


def test_signed_by_the_expected_ca(ca_pem):
    ca = SshUserCa(ca_pem)
    _, cert = ca.mint("agent-host")
    ca_key = asyncssh.import_private_key(ca_pem)
    assert cert.signing_key.public_data == ca_key.convert_to_public().public_data


def test_rejects_a_public_key_as_the_ca(tmp_path):
    path = tmp_path / "ca"
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(path)],
        check=True,
        capture_output=True,
    )
    with pytest.raises(ValueError, match="not a usable private key"):
        SshUserCa((path.with_suffix(".pub")).read_text())


def test_rejects_a_non_ed25519_ca(tmp_path):
    """The constructor is the one chokepoint that ever sees the CA key -- a
    1024-bit RSA key loads and would sign happily otherwise (empirically
    checked against asyncssh directly), so it is where a wrong key type must
    be refused, with a message naming both what was required and supplied.
    """
    path = tmp_path / "ca"
    subprocess.run(
        ["ssh-keygen", "-t", "rsa", "-b", "1024", "-N", "", "-f", str(path)],
        check=True,
        capture_output=True,
    )
    with pytest.raises(ValueError) as exc_info:
        SshUserCa(path.read_text())
    message = str(exc_info.value)
    assert "ed25519" in message.lower()
    assert "ssh-rsa" in message


@pytest.mark.parametrize("bad", [0, -1, -60, -1000])
def test_mint_refuses_a_non_positive_lifetime(ca_pem, bad):
    """asyncssh only rejects part of this range on its own: with the 60s
    backdate, a lifetime in (-CERT_BACKDATE_SECONDS, 0] still leaves
    valid_before > valid_after, so it mints a certificate that is already past
    its own valid_before — unusable, but not discovered until validate().
    Bound it here instead, so the failure names the real cause."""
    ca = SshUserCa(ca_pem)
    with pytest.raises(ValueError, match="must be positive"):
        ca.mint("agent-host", lifetime_seconds=bad)
