# tests/test_ssh_gateway_ca.py
import subprocess
import time

import asyncssh
import pytest
from asyncssh.public_key import CERT_TYPE_USER

from services.ssh_gateway_ca import DEFAULT_CERT_LIFETIME_SECONDS, SshUserCa


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


def test_certificate_is_short_lived(ca_pem):
    _, cert = SshUserCa(ca_pem).mint("agent-host")
    # asyncssh 2.24.0's SSHOpenSSHCertificate(V01) stores the validity window
    # only as `_valid_after`/`_valid_before` (see public_key.py:1572-1573).
    # There is no public `valid_after`/`valid_before` property in this
    # version -- confirmed by reading the installed source. See
    # task-1-report.md for the full discrepancy note.
    span = cert._valid_before - cert._valid_after
    assert span <= DEFAULT_CERT_LIFETIME_SECONDS + 120  # clock-skew allowance


def test_certificate_is_valid_now(ca_pem):
    _, cert = SshUserCa(ca_pem).mint("agent-host")
    now = time.time()
    assert cert._valid_after <= now < cert._valid_before


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


def test_each_mint_uses_a_fresh_keypair(ca_pem):
    """A per-connection keypair means a leaked one is worthless in minutes and
    cannot be correlated across sessions."""
    ca = SshUserCa(ca_pem)
    first, _ = ca.mint("agent-host")
    second, _ = ca.mint("agent-host")
    assert first.export_private_key() != second.export_private_key()


def test_mint_grants_no_extensions(ca_pem):
    """asyncssh defaults permit-pty/X11/agent/port-forwarding/user-rc to
    granted (True) unless told otherwise (public_key.py:657-661) -- the
    opposite of "no extensions". mint() must deny every one explicitly."""
    _, cert = SshUserCa(ca_pem).mint("agent-host")
    assert cert.options == {}


def test_signed_by_the_expected_ca(ca_pem, tmp_path):
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
    with pytest.raises(ValueError):
        SshUserCa((path.with_suffix(".pub")).read_text())
