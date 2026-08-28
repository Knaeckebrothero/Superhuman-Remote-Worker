"""Short public handles that address a session over SSH.

The handle is the SSH *username*: `ssh s-7f3a91c2@ssh.<domain>`. It is also
written verbatim into the user's ``~/.ssh/config``, so its charset is a
security boundary for config injection — validate on output, not only at mint.

Unguessability is a bonus, not a control. Authorization is the control.
"""

from __future__ import annotations

import re
import secrets

# Crockford base32 minus the ambiguous i/l/o/u.
_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"
_LENGTH = 8

HANDLE_PATTERN = re.compile(r"^s-[0-9abcdefghjkmnpqrstvwxyz]{8}$")


def mint_ssh_handle() -> str:
    """A fresh handle. 32**8 ≈ 1.1e12 values; collisions are handled by the
    unique constraint plus a retry at the call site."""
    return "s-" + "".join(secrets.choice(_ALPHABET) for _ in range(_LENGTH))


def is_valid_handle(value: str) -> bool:
    """True iff ``value`` is safe to emit into generated SSH config."""
    return bool(value) and HANDLE_PATTERN.fullmatch(value) is not None
