"""The gateway's SSH user certificate authority.

The gateway does NOT hold the cluster-wide workspace key. It holds a CA, and
mints a fresh keypair plus a certificate valid for minutes on every inbound
connection. Consequences worth keeping in mind while editing this file:

- Compromising the gateway yields the ability to mint until the CA is rotated,
  not possession of a standing credential that opens every workspace forever.
- Revocation is publishing a new CA public key to workspaces, not
  re-provisioning the fleet.

The single principal is load-bearing, but only within one scope: it restricts
the certificate to one Unix *user* on the inner hop. A certificate whose
principal list is empty or wildcarded would be accepted for any user on any
host -- that class of mistake is what CVE-2025-49825 was -- but a correctly
scoped single principal does not by itself restrict *which workspace* accepts
it. Every workspace image bakes in the same Unix user
(``docker/Dockerfile.workspace``'s ``useradd -m -s /bin/bash -u 1000
agent-host``), and OpenSSH's ``AuthorizedPrincipalsFile`` defaults to
``none``, so today a certificate minted for one workspace is honored by every
workspace for the whole of its validity window. Closing that is Task 9's to
do: per-workspace principals plus an ``AuthorizedPrincipalsFile`` that only
the intended workspace populates. It is NOT ``source_address`` -- that option
restricts the address the certificate's *presenter* connects from
(``ssh-keygen(1)``: "the source addresses from which the certificate is
considered valid"), and our presenter is the gateway, not the workspace, so
pinning a target workspace's pod IP there would make every legitimate
connection invalid rather than scope anything.

The certificate carries exactly two OpenSSH extensions -- ``permit-pty`` and
``permit-port-forwarding`` -- and denies the other three (``permit-X11-
forwarding``, ``permit-agent-forwarding``, ``permit-user-rc``). Extensions are
permissive-by-listing: an omitted one is refused for the life of the
certificate no matter what the workspace's own ``sshd_config`` allows, so
``sshd_config`` cannot grant back a permission this certificate withholds.
Denying ``permit-pty`` would mean no interactive shell at all -- the feature's
primary use case -- and denying ``permit-port-forwarding`` would kill the
``direct-tcpip`` path JetBrains Gateway/``ProxyJump`` needs and that the
gateway's own ``clamp_direct_tcpip`` exists to police. Two settings in the
workspace's ``sshd_config`` (``docker/Dockerfile.workspace``) do the real
narrowing once the certificate permits forwarding at all: ``PermitOpen
127.0.0.1:*`` constrains *where* a ``direct-tcpip`` channel may target, and
``AllowTcpForwarding local`` denies remote (``-R``) forwarding outright.
Relaxing either widens what this certificate's ``permit-port-forwarding``
actually grants, so a future editor touching one should see the other. Both
asyncssh's ``generate_user_certificate`` and real OpenSSH's ``ssh-keygen -s``
grant every ``permit-*`` extension by default when unspecified (the latter's
``-O clear`` option exists precisely because the default is all-on -- see
``ssh-keygen(1)``), so ``mint()`` below must deny X11/agent-forwarding/
user-rc explicitly; it cannot rely on omission. Agent forwarding into an
agent's own workspace is a real risk on top of being merely unneeded, X11 is
unused here, and ``permit-user-rc`` is extra execution surface for nothing.
"""

from __future__ import annotations

import time

import asyncssh

# Not exported from asyncssh's top level: `__all__` carries only the base
# SSHCertificate, which does NOT define validate()/principals/options. This
# submodule path is therefore the only way to annotate what mint() really
# returns; tests/test_ssh_gateway_ca.py already imports CERT_TYPE_USER from
# the same place. If a future asyncssh relocates it, this fails loudly at
# import rather than silently.
from asyncssh.public_key import SSHOpenSSHCertificate

DEFAULT_CERT_LIFETIME_SECONDS = 300
# Neither asyncssh nor OpenSSH impose a ceiling of their own -- an unspecified
# valid_before means "forever" to both -- so this module owns one. A multiple
# of the default (rather than an unrelated round number) keeps the
# relationship between "normal" and "as long as we ever allow" visible at the
# call site.
MAX_CERT_LIFETIME_SECONDS = DEFAULT_CERT_LIFETIME_SECONDS * 4
# How far before "now" `valid_after` is backdated, so a workspace whose clock
# trails the gateway's does not reject a certificate issued moments ago.
# Named so tests can pin the certificate's span exactly instead of allowing
# slop for an unlabeled number.
CERT_BACKDATE_SECONDS = 60


class SshUserCa:
    """Mints short-lived user certificates for the inner hop."""

    def __init__(self, ca_private_key_pem: str | bytes):
        """Load and validate the CA's signing key.

        Restricted to Ed25519. This constructor is the one place that ever
        touches the CA key, making it the natural chokepoint to reject a
        weak or wrong-type key -- e.g. asyncssh will happily load and sign
        with a 1024-bit RSA key otherwise -- before it can mint anything.
        """
        try:
            self._ca_key = asyncssh.import_private_key(ca_private_key_pem)
        except Exception as exc:
            raise ValueError("ssh-gateway user CA is not a usable private key") from exc
        algorithm = self._ca_key.get_algorithm()
        if algorithm != "ssh-ed25519":
            raise ValueError(
                f"ssh-gateway user CA must be Ed25519 (ssh-ed25519), got {algorithm!r}"
            )

    @property
    def public_key_line(self) -> str:
        """The `TrustedUserCAKeys` line workspaces must trust."""
        return self._ca_key.convert_to_public().export_public_key().decode().strip()

    def mint(
        self,
        principal: str,
        lifetime_seconds: int = DEFAULT_CERT_LIFETIME_SECONDS,
    ) -> tuple[asyncssh.SSHKey, SSHOpenSSHCertificate]:
        """A fresh keypair and a certificate over it, valid for one attachment.

        ``valid_after`` is backdated by ``CERT_BACKDATE_SECONDS`` (see that
        constant's docstring). ``lifetime_seconds`` is bounded on BOTH ends,
        explicitly, rather than leaning on asyncssh to reject the degenerate
        cases. It does not reject all of them: for a lifetime in
        ``(-CERT_BACKDATE_SECONDS, 0]`` the backdating still leaves
        ``valid_before > valid_after``, so asyncssh happily mints a
        certificate that is already past its own ``valid_before`` by the time
        the call returns -- unusable, but only discovered downstream at
        ``validate()``. Only a lifetime at or below
        ``-CERT_BACKDATE_SECONDS`` trips asyncssh's own guard. The top end has
        no guard at all, so a future caller wiring this to config or an env
        var could otherwise mint a certificate lasting years from one typo.

        ``permit_pty`` and ``permit_port_forwarding`` are granted -- an
        interactive shell needs the former, ``direct-tcpip`` needs the
        latter, and the workspace's own ``PermitOpen 127.0.0.1:*`` /
        ``AllowTcpForwarding local`` narrow what that forwarding can reach
        and whether it can run in reverse. The other three ``permit_*``
        flags are passed as ``False`` explicitly: asyncssh's own default for
        each is ``True`` (confirmed against the installed 2.24.0 source, and
        matching real ``ssh-keygen -s``'s documented default), so omitting
        them would silently grant X11 forwarding, agent forwarding, and the
        user rc file -- none of which this certificate should carry.
        """
        if not principal:
            raise ValueError("a certificate principal is required")
        if lifetime_seconds <= 0:
            raise ValueError(
                f"lifetime_seconds={lifetime_seconds} must be positive; "
                "a non-positive lifetime mints an already-expired certificate"
            )
        if lifetime_seconds > MAX_CERT_LIFETIME_SECONDS:
            raise ValueError(
                f"lifetime_seconds={lifetime_seconds} exceeds "
                f"MAX_CERT_LIFETIME_SECONDS={MAX_CERT_LIFETIME_SECONDS}"
            )
        key = asyncssh.generate_private_key("ssh-ed25519")
        now = int(time.time())
        cert = self._ca_key.generate_user_certificate(
            key,
            "srw-ssh-gateway",
            principals=[principal],
            valid_after=now - CERT_BACKDATE_SECONDS,
            valid_before=now + lifetime_seconds,
            permit_pty=True,
            permit_port_forwarding=True,
            permit_x11_forwarding=False,
            permit_agent_forwarding=False,
            permit_user_rc=False,
        )
        return key, cert


def load_user_ca(path: str) -> SshUserCa:
    """Load the CA from its mounted secret.

    Read as BYTES, not utf-8 text: asyncssh accepts DER as well as PEM, and a
    text-mode read turns a perfectly good binary key into a UnicodeDecodeError
    that blames the key rather than the encoding. ssh_gateway_config's
    host-key check carried the identical bug and was fixed the same way.

    Re-raises the constructor's ValueError with the path attached.
    ``SshUserCa.__init__`` deliberately does not know its own path -- it takes
    a key so it stays usable with an in-memory one -- but an operator reading a
    startup failure needs to know WHICH file was wrong, which is what
    ``_require_ed25519_host_key`` already does in every one of its branches.
    """
    with open(path, "rb") as handle:
        data = handle.read()
    try:
        return SshUserCa(data)
    except ValueError as exc:
        raise ValueError(f"{path!r}: {exc}") from (exc.__cause__ or exc)
