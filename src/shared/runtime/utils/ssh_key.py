"""Normalize and validate PEM-encoded SSH private keys.

The original symptom that motivated this module: a private key submitted
through the Cockpit form arrived on the workspace pod missing its trailing
newline. The bytes between BEGIN/END were byte-perfect, but OpenSSL/libcrypto
rejects PEM blobs without the final ``\\n`` ("error in libcrypto"), so every
``git clone`` over SSH failed. Normalizing on save (and again on consume)
prevents the same shape of bug from recurring for any other ssh-key-backed
datasource.
"""

from __future__ import annotations

import base64
import re
from typing import NamedTuple


_BEGIN_MARKERS = (
    "-----BEGIN OPENSSH PRIVATE KEY-----",
    "-----BEGIN RSA PRIVATE KEY-----",
    "-----BEGIN EC PRIVATE KEY-----",
    "-----BEGIN DSA PRIVATE KEY-----",
    "-----BEGIN PRIVATE KEY-----",
    "-----BEGIN ENCRYPTED PRIVATE KEY-----",
)
_END_MARKERS = (
    "-----END OPENSSH PRIVATE KEY-----",
    "-----END RSA PRIVATE KEY-----",
    "-----END EC PRIVATE KEY-----",
    "-----END DSA PRIVATE KEY-----",
    "-----END PRIVATE KEY-----",
    "-----END ENCRYPTED PRIVATE KEY-----",
)
_BEGIN_END_PAIRS = dict(zip(_BEGIN_MARKERS, _END_MARKERS))

# PEM headers (e.g. "Proc-Type: 4,ENCRYPTED") sit between BEGIN and the
# base64 body, separated from the body by a blank line. We only allow the
# common encrypted-PEM headers here so a random non-header line in the body
# region gets caught by validation.
_PEM_HEADER_RE = re.compile(r"^[A-Za-z0-9-]+:\s.*$")


class InvalidSSHKeyError(ValueError):
    """Raised when a submitted SSH private key fails structural validation."""


def normalize_private_key(key: str) -> str:
    """Return a normalized copy of ``key`` suitable for writing to disk.

    - Converts CRLF / CR line endings to LF.
    - Strips leading and trailing whitespace.
    - Ensures exactly one trailing newline.

    Returns the input unchanged if it is empty or not a string — validation
    of those cases is the caller's job.
    """
    if not isinstance(key, str) or not key:
        return key
    normalized = key.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return ""
    return normalized + "\n"


def validate_private_key(key: str) -> str:
    """Normalize ``key`` and confirm it looks like a PEM private key.

    Checks performed:
    1. Non-empty string.
    2. First line is a recognized ``-----BEGIN ... PRIVATE KEY-----`` marker.
    3. Last non-empty line is the matching ``-----END ... PRIVATE KEY-----``.
    4. Lines between the markers are either PEM headers (key: value) or
       base64 characters only, and the concatenated body decodes cleanly.

    On success returns the normalized key (single trailing newline, LF
    endings). On failure raises :class:`InvalidSSHKeyError` with a message
    suitable for surfacing to the user.

    This is a *structural* check, not a cryptographic one — it catches the
    common formatting bugs (truncation, missing trailing newline, pasted
    junk) without pulling in a heavy crypto dependency. The clone itself
    still validates the key cryptographically when git connects to the
    remote.
    """
    if not isinstance(key, str) or not key.strip():
        raise InvalidSSHKeyError("SSH private key is empty")

    normalized = normalize_private_key(key)
    lines = normalized.rstrip("\n").split("\n")
    if len(lines) < 3:
        raise InvalidSSHKeyError(
            "SSH private key is too short — expected BEGIN/body/END lines"
        )

    begin = lines[0]
    end = lines[-1]
    if begin not in _BEGIN_END_PAIRS:
        raise InvalidSSHKeyError(
            f"SSH private key missing recognized BEGIN marker (got {begin!r})"
        )
    expected_end = _BEGIN_END_PAIRS[begin]
    if end != expected_end:
        raise InvalidSSHKeyError(
            f"SSH private key END marker {end!r} does not match BEGIN "
            f"(expected {expected_end!r})"
        )

    # Split inner lines into optional PEM headers (terminated by a blank
    # line) and the base64 body.
    inner = lines[1:-1]
    body_start = 0
    if inner and _PEM_HEADER_RE.match(inner[0]):
        for i, line in enumerate(inner):
            if line == "":
                body_start = i + 1
                break
        else:
            raise InvalidSSHKeyError(
                "SSH private key has PEM headers but no blank line before the body"
            )
    body_lines = inner[body_start:]
    body = "".join(body_lines)
    if not body:
        raise InvalidSSHKeyError("SSH private key has no base64 body")

    try:
        base64.b64decode(body, validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise InvalidSSHKeyError(
            f"SSH private key base64 body is invalid: {exc}"
        ) from exc

    return normalized


class GeneratedKeypair(NamedTuple):
    """The two halves of a freshly generated SSH keypair.

    ``private_key`` is the OpenSSH PEM (BEGIN/END framed, normalized with a
    trailing newline). ``public_key`` is the single-line OpenSSH authorized-
    keys format (``ssh-ed25519 AAAA... <comment>``) ready to paste into
    GitHub/Gitea/GitLab deploy-key fields.
    """

    private_key: str
    public_key: str


def generate_ed25519_keypair(comment: str = "") -> GeneratedKeypair:
    """Generate a fresh ed25519 SSH keypair.

    ed25519 is the modern default (small, fast, fixed-size, ECC-based) and
    accepted by every mainstream git host. The private key is emitted in
    OpenSSH PEM format with no passphrase — that's what the agent expects
    when it writes the key into the workspace container.

    The optional ``comment`` is appended to the public key (after a space)
    so the user can identify the key later in a provider's deploy-keys UI.
    Comments are stripped of newlines and tabs before embedding to keep the
    public key on a single line.
    """
    # Imported lazily so the module is importable in environments that don't
    # have ``cryptography`` available (e.g. minimal test envs that only use
    # the normalize/validate helpers).
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
        PublicFormat,
    )

    key = Ed25519PrivateKey.generate()
    private_bytes = key.private_bytes(
        encoding=Encoding.PEM,
        format=PrivateFormat.OpenSSH,
        encryption_algorithm=NoEncryption(),
    )
    public_bytes = key.public_key().public_bytes(
        encoding=Encoding.OpenSSH,
        format=PublicFormat.OpenSSH,
    )

    private_pem = normalize_private_key(private_bytes.decode("ascii"))
    public_line = public_bytes.decode("ascii").strip()
    safe_comment = (comment or "").replace("\n", " ").replace("\t", " ").strip()
    if safe_comment:
        public_line = f"{public_line} {safe_comment}"
    return GeneratedKeypair(private_key=private_pem, public_key=public_line)


__all__ = [
    "GeneratedKeypair",
    "InvalidSSHKeyError",
    "generate_ed25519_keypair",
    "normalize_private_key",
    "validate_private_key",
]
