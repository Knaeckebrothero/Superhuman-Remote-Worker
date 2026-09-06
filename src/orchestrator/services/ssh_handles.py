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

# Derived from _ALPHABET/_LENGTH rather than spelled out separately: a
# hand-copied second charset here could drift from the minter's and still
# agree in every test until the day it accepts a character the minter never
# emits. Anchored with \Z, not $ — re's $ matches immediately before a
# trailing newline, so a $-anchored pattern's .match()/.search() would accept
# a handle with "\n" appended (fullmatch is unaffected). \Z has no such
# exception, so this stays safe under match/search/fullmatch alike — load-
# bearing because HANDLE_PATTERN is exported and used directly, not only
# through is_valid_handle's fullmatch.
HANDLE_PATTERN = re.compile(rf"^s-[{re.escape(_ALPHABET)}]{{{_LENGTH}}}\Z")


def mint_ssh_handle() -> str:
    """A fresh handle. 32**8 ≈ 1.1e12 values, so a same-candidate collision is
    exceedingly unlikely — but the two call sites treat one differently: the
    ``create_thread`` INSERT relies on the unique index alone and lets a
    collision fail the insert outright, while the lazy backfill
    (``PostgresDB.ensure_thread_ssh_handle``) retries on the unique
    constraint instead."""
    return "s-" + "".join(secrets.choice(_ALPHABET) for _ in range(_LENGTH))


def is_valid_handle(value: str) -> bool:
    """True iff ``value`` is safe to emit into generated SSH config."""
    return bool(value) and HANDLE_PATTERN.fullmatch(value) is not None
