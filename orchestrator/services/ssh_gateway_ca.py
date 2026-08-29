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

The certificate also carries no OpenSSH extensions (permit-pty,
permit-X11-forwarding, permit-agent-forwarding, permit-port-forwarding,
permit-user-rc). Unlike the ``ssh-keygen -s`` CLI -- which grants none of
these unless asked -- asyncssh's ``generate_user_certificate`` defaults every
one of them to ``True``, so ``mint()`` below denies each explicitly rather
than omitting them; omitting them would silently grant all five. This
certificate only has to satisfy the workspace's ``TrustedUserCAKeys``; the
workspace's own ``sshd_config`` already governs what a session may do, and
this certificate should not duplicate or widen that.
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

        Every ``permit_*`` extension is passed as ``False``: asyncssh's own
        default for each is ``True`` (confirmed against the installed 2.24.0
        source), so leaving them out would grant pty/X11-forwarding/
        agent-forwarding/port-forwarding and the user rc file -- the opposite
        of the "no extensions" this certificate is meant to carry.
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
            permit_x11_forwarding=False,
            permit_agent_forwarding=False,
            permit_port_forwarding=False,
            permit_pty=False,
            permit_user_rc=False,
        )
        return key, cert


def load_user_ca(path: str) -> SshUserCa:
    """Load the CA from its mounted secret."""
    with open(path, encoding="utf-8") as handle:
        return SshUserCa(handle.read())
