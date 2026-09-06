"""Parsing and policy for user-registered SSH public keys.

Pure functions, no I/O. The fingerprint this module produces is the identity
the ssh-gateway presents to the orchestrator, so its format is load-bearing:
it is asyncssh's ``SHA256:base64`` form, byte-identical to what
``ssh-keygen -lf`` prints locally.

Policy notes:

- ``ssh-dss`` is removed in OpenSSH 10.0 but asyncssh still parses it, so the
  rejection has to be explicit.
- ``ssh-ed448`` is supported by asyncssh and NOT by OpenSSH; accepting it would
  let a user register a key no client of theirs can use.
- RSA < 3072 is a deliberate policy floor, not a compliance requirement. NIST
  permits verification at >=2048 indefinitely; we are stricter because
  ``ssh-keygen`` has defaulted to 3072 since OpenSSH 8.0.
- ``sk-*`` types are FIDO2 hardware-backed keys. asyncssh enforces the
  user-presence flag by default; we simply must not honour ``no-touch-required``.
"""

from __future__ import annotations

from dataclasses import dataclass

import asyncssh
from asyncssh.sshsig import import_allowed_signers, validate_sshsig

SIGNATURE_NAMESPACE = "srw-ssh-key-registration"

MINIMUM_RSA_BITS = 3072

ACCEPTED_KEY_TYPES = frozenset(
    {
        "ssh-ed25519",
        "sk-ssh-ed25519@openssh.com",
        "sk-ecdsa-sha2-nistp256@openssh.com",
        "ecdsa-sha2-nistp256",
        "ecdsa-sha2-nistp384",
        "ecdsa-sha2-nistp521",
        "ssh-rsa",
    }
)


class SshKeyRejected(ValueError):
    """A pasted key failed policy. ``reason`` is safe to show the user."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ParsedSshKey:
    key_type: str
    public_key: str
    fingerprint_sha256: str
    comment: str


def parse_public_key(text: str) -> ParsedSshKey:
    """Parse one ``authorized_keys`` line, applying type and strength policy."""
    candidate = (text or "").strip()
    if not candidate:
        raise SshKeyRejected("Paste an SSH public key.")
    if "PRIVATE KEY" in candidate:
        raise SshKeyRejected(
            "That is a private key. Paste the matching .pub file instead, and "
            "rotate this key — it should not have left your machine."
        )
    if len(candidate.splitlines()) != 1:
        raise SshKeyRejected("Paste exactly one key, on a single line.")

    try:
        key = asyncssh.import_public_key(candidate)
    except Exception:  # asyncssh raises several unrelated types here
        raise SshKeyRejected("That does not look like an SSH public key.") from None

    key_type = key.get_algorithm()
    if key_type not in ACCEPTED_KEY_TYPES:
        raise SshKeyRejected(f"{key_type} keys are not accepted.")
    if key_type == "ssh-rsa":
        bits = _rsa_bits(key)
        if bits < MINIMUM_RSA_BITS:
            raise SshKeyRejected(
                f"RSA keys must be at least {MINIMUM_RSA_BITS} bits; this one is {bits}."
            )

    parts = candidate.split(None, 2)
    comment = parts[2].strip() if len(parts) > 2 else ""

    return ParsedSshKey(
        key_type=key_type,
        public_key=candidate,
        fingerprint_sha256=key.get_fingerprint("sha256"),
        comment=comment,
    )


def _rsa_bits(key) -> int:
    """RSA modulus size. asyncssh exposes no stable public accessor, so read the
    wire encoding: an RSA public key is (algorithm, e, n) and ``n`` is last."""
    from asyncssh.packet import SSHPacket

    packet = SSHPacket(key.public_data)
    packet.get_string()  # algorithm
    packet.get_mpint()  # e
    return packet.get_mpint().bit_length()


def verify_possession(
    public_key: str, namespace: str, payload: bytes, signature: str
) -> bool:
    """True iff ``signature`` is an ``ssh-keygen -Y sign`` signature over
    ``payload`` in ``namespace``, made by the private half of ``public_key``.

    Registration without this check would let anyone claim a public key they
    merely read (they are published at, e.g., ``github.com/<user>.keys``), and
    because fingerprints are globally unique that turns into a denial of service
    against the rightful owner.
    """
    # asyncssh has no top-level verify()/read_ssh_signature(); SSHSIG lives in
    # asyncssh.sshsig. Two traps, both verified by probe against 2.24.0:
    #   * `sig` and `allowed_signers` given as `str` are read as FILE PATHS, so an
    #     inline signature raises FileNotFoundError with the signature in the message.
    #     Pass sig as bytes and build allowed_signers via import_allowed_signers().
    #   * The namespace is bound through the allowed_signers `namespaces=` option,
    #     not a parameter.
    principal = "srw"
    try:
        key_fields = " ".join(public_key.split()[:2])
        allowed = import_allowed_signers(
            f'{principal} namespaces="{namespace}" {key_fields}\n'
        )
        sig_bytes = signature.encode() if isinstance(signature, str) else signature
        return bool(validate_sshsig(payload, sig_bytes, principal, allowed))
    except Exception:
        return False
