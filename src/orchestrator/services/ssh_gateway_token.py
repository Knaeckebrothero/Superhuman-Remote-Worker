"""The short-lived, user-bound credential a USER presents to the WSS front door.

WHY THIS IS NOT ``MCP_INTERNAL_KEY`` (ruling G38). The plan's original
``_token_valid()`` compared the presented bearer token to
``config.internal_key``, which ``ssh_gateway_config`` sources from
``MCP_INTERNAL_KEY`` — the same value the gateway sends as ``X-Internal-Key``
to the orchestrator's privileged internal API, and which ``require_internal``
guards roughly fifty endpoints with. Plan 3's client helper keeps its token in
``~/.config/srw/token`` on a user's laptop, so that design would have
distributed the platform's master service credential to every SSH user, in a
world-readable-by-default location, with no expiry and no per-user revocation.
A user-facing credential and a service-to-service credential are not the same
credential; §6.3 of the design warns about exactly this seam twice, citing
Gitpod's CVE-2023-0957 and Daytona's CVE-2026-54324. The gateway still holds
``MCP_INTERNAL_KEY`` for its OWN outbound calls — only the credential the user
presents changed.

CONSTRUCTION IS DELIBERATELY NOT NOVEL. It is the same stateless HMAC-SHA256
token plan 1 already ships for SSH-key registration
(``main._mint_ssh_key_challenge`` / ``_verify_ssh_key_challenge``): a version
clause, a nonce, the user id, an expiry and a hex MAC over all of them, keyed
with ``SESSION_JWT_SECRET`` and colon-joined. Stateless is a requirement, not
a shortcut: the orchestrator runs ``orchestrator.replicas: 2`` with no session
affinity and the gateway is a *separate Deployment* from the minter, so there
is no shared memory anywhere on this path. Every replica shares
``SESSION_JWT_SECRET`` through one Kubernetes Secret; none of them share a
dict. Do not "improve" this into a nonce store.

TWO DELIBERATE DIFFERENCES FROM THE REGISTRATION CHALLENGE:

* A different version clause, ``srw-sshws1``. Both tokens are MACs under the
  same key, so this clause — inside the MAC'd head — is the only thing that
  keeps them apart. Without it, every user who ever requested a key
  registration challenge would be holding a gateway credential, and every
  gateway token would register keys. Both directions are pinned in
  tests/test_ssh_gateway_token.py.
* No identity label. The challenge carries a fifth, display-only clause
  because a human reads that token before signing it with ``ssh-keygen -Y
  sign``; nobody eyeballs this one, so a label would be an attack surface
  (it must stay single-line printable ASCII) with no anti-phishing payoff.

WHAT THIS TOKEN IS AND IS NOT. It authenticates the *transport*: it proves an
approved SRW user asked the control plane for a way in, within the last few
minutes. It is NOT the authorization decision — the SSH layer still
authenticates by public key and the orchestrator still resolves the target by
fingerprint, independently of who fetched the token. It also binds to a user,
not to a workspace handle, because the handle is the SSH username and is not
known until after the SSH handshake, which happens *inside* this transport.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from typing import Optional

# Distinct from main's ``srw-ssh1``. See the module docstring: this string is
# the entire domain separation between two tokens signed with one key.
ATTACH_TOKEN_VERSION = "srw-sshws1"

# Matches _SSH_CHALLENGE_TTL_SECONDS. Long enough to survive a user fetching a
# token and then running ssh, plus modest clock skew between the orchestrator
# and gateway pods; short enough that a token captured from a laptop's
# filesystem is worthless within minutes. The token is checked once, at the
# WebSocket handshake — an established session is not torn down when its token
# expires, exactly like the SSH certificate the inner hop mints.
ATTACH_TOKEN_TTL_SECONDS = 300

# Bounds the work an unauthenticated header value can buy before the MAC runs.
# A minted token is ~110 bytes; the cap mirrors ``SshKeyCreate.challenge``'s
# own ``max_length=1024``.
ATTACH_TOKEN_MAX_LENGTH = 1024


def mint_attach_token(
    user_id: str, secret: str, *, now: Optional[float] = None
) -> tuple[str, float]:
    """Mint an attach token bound to ``user_id``. Returns ``(token, expires_at)``.

    Fails closed on an empty secret rather than signing with a well-known
    empty key — the precondition lives on the reusable unit, not only on its
    HTTP caller, because this is called from ``main`` where a 503 makes sense
    and could later be called from somewhere no HTTP status exists.
    """
    if not secret:
        raise RuntimeError(
            "SESSION_JWT_SECRET is empty; refusing to mint a forgeable SSH "
            "attach token."
        )
    if now is None:
        now = time.time()
    expires_at = now + ATTACH_TOKEN_TTL_SECONDS
    nonce = secrets.token_urlsafe(24)
    head = f"{ATTACH_TOKEN_VERSION}:{nonce}:{user_id}:{int(expires_at)}"
    return f"{head}:{_sign(head, secret)}", expires_at


def verify_attach_token(
    token: object, secret: str, *, now: Optional[float] = None
) -> Optional[str]:
    """The user id ``token`` was minted for, or ``None``.

    Returns the id rather than a bool so a caller cannot forget to ask WHO
    authenticated; the gateway logs it, and a future authorization step can
    use it.

    Check order matches ``_verify_ssh_key_challenge`` and is load-bearing:

    1. It is a ``str``, within the length cap, and ASCII.
       ``hmac.compare_digest`` raises ``TypeError`` on a non-ASCII ``str``,
       and a lone UTF-16 surrogate raises ``UnicodeEncodeError`` on
       ``.encode()`` — both reachable pre-authentication from a raw
       ``Authorization`` header, where an unhandled exception would turn a
       clean 4401 close into a 500-equivalent an anonymous client could loop.
    2. The secret is configured — verifying against an empty key would accept
       a forgery anyone can compute.
    3. It parses at all, and its version clause is ours (domain separation).
    4. The MAC, in constant time, BEFORE any field the token carries is
       trusted: until it checks out, the expiry and the user id are
       attacker-controlled strings, not facts.
    5. Expiry.
    """
    if not isinstance(token, str):
        return None
    if not token or len(token) > ATTACH_TOKEN_MAX_LENGTH:
        return None
    if not token.isascii():
        return None
    if not secret:
        return None
    if now is None:
        now = time.time()

    try:
        head, signature = token.rsplit(":", 1)
        version, _nonce, user_id, expires_at_raw = head.split(":", 3)
    except ValueError:
        return None
    if version != ATTACH_TOKEN_VERSION:
        return None
    if not hmac.compare_digest(_sign(head, secret), signature):
        return None
    try:
        expires_at = float(expires_at_raw)
    except ValueError:
        return None
    if expires_at <= now:
        return None
    return user_id


def _sign(head: str, secret: str) -> str:
    return hmac.new(
        secret.encode("utf-8"), head.encode("utf-8"), hashlib.sha256
    ).hexdigest()
