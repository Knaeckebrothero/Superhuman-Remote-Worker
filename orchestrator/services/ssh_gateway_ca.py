"""The gateway's SSH user certificate authority.

The gateway does NOT hold the cluster-wide workspace key. It holds a CA, and
mints a fresh keypair plus a certificate valid for minutes on every inbound
connection. Consequences worth keeping in mind while editing this file:

- Compromising the gateway yields the ability to mint until the CA is rotated,
  not possession of a standing credential that opens every workspace forever.
- Revocation is publishing a new CA public key to workspaces, not
  re-provisioning the fleet.

The single principal is load-bearing. A certificate whose principal list is
empty or wildcarded would be accepted for any user on any host — that class of
mistake is what CVE-2025-49825 was.

The certificate carries exactly two OpenSSH extensions -- ``permit-pty`` and
``permit-port-forwarding`` -- and denies the other three (``permit-X11-
forwarding``, ``permit-agent-forwarding``, ``permit-user-rc``). Extensions are
permissive-by-listing: an omitted one is refused for the life of the
certificate no matter what the workspace's own ``sshd_config`` allows, so
``sshd_config`` cannot grant back a permission this certificate withholds.
Denying ``permit-pty`` would mean no interactive shell at all -- the feature's
primary use case -- and denying ``permit-port-forwarding`` would kill the
``direct-tcpip`` path JetBrains Gateway/``ProxyJump`` needs and that the
gateway's own ``clamp_direct_tcpip`` exists to police; the workspace's
``PermitOpen 127.0.0.1:*`` does the real narrowing of *where* that forwarding
can go, but only once the certificate permits forwarding at all. Both
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

DEFAULT_CERT_LIFETIME_SECONDS = 300


class SshUserCa:
    """Mints short-lived user certificates for the inner hop."""

    def __init__(self, ca_private_key_pem: str):
        try:
            self._ca_key = asyncssh.import_private_key(ca_private_key_pem)
        except Exception as exc:
            raise ValueError("ssh-gateway user CA is not a usable private key") from exc

    @property
    def public_key_line(self) -> str:
        """The `TrustedUserCAKeys` line workspaces must trust."""
        return self._ca_key.convert_to_public().export_public_key().decode().strip()

    def mint(
        self,
        principal: str,
        lifetime_seconds: int = DEFAULT_CERT_LIFETIME_SECONDS,
    ) -> tuple[asyncssh.SSHKey, asyncssh.SSHCertificate]:
        """A fresh keypair and a certificate over it, valid for one attachment.

        ``valid_after`` is backdated 60s so a workspace whose clock trails the
        gateway does not reject a certificate issued moments ago.

        ``permit_pty`` and ``permit_port_forwarding`` are granted -- an
        interactive shell needs the former, ``direct-tcpip`` needs the
        latter, and the workspace's own ``PermitOpen 127.0.0.1:*`` narrows
        what that forwarding can reach. The other three ``permit_*`` flags
        are passed as ``False`` explicitly: asyncssh's own default for each
        is ``True`` (confirmed against the installed 2.24.0 source, and
        matching real ``ssh-keygen -s``'s documented default), so omitting
        them would silently grant X11 forwarding, agent forwarding, and the
        user rc file -- none of which this certificate should carry.
        """
        if not principal:
            raise ValueError("a certificate principal is required")
        key = asyncssh.generate_private_key("ssh-ed25519")
        now = int(time.time())
        cert = self._ca_key.generate_user_certificate(
            key,
            "srw-ssh-gateway",
            principals=[principal],
            valid_after=now - 60,
            valid_before=now + lifetime_seconds,
            permit_pty=True,
            permit_port_forwarding=True,
            permit_x11_forwarding=False,
            permit_agent_forwarding=False,
            permit_user_rc=False,
        )
        return key, cert


def load_user_ca(path: str) -> SshUserCa:
    """Load the CA from its mounted secret."""
    with open(path, encoding="utf-8") as handle:
        return SshUserCa(handle.read())
