"""Per-workspace code-server credentials — recipient binding for the IDE proxy.

A Pod IP is a routing coordinate, not an identity: Kubernetes may delete
runtime A and reuse its address for unrelated runtime B, and no amount of
control-plane attestation closes that window, because the authority consulted
(API-server Pod status) lags the authority that actually assigns the address
(the CNI). Every other caller repaired on this boundary solved it the same
way: the request carries an expected identity and *the receiver validates
it*. The IDE could not, because code-server ran ``auth: none`` — our own
image default, not a property of code-server.

So bind it. Each workspace runs code-server with a credential only this
orchestrator can derive, and the proxy presents that credential upstream. A
connection that lands on a foreign runtime meets a different credential and is
refused **at the destination**, whatever the control plane believed.

Derivation, not storage. The credential is an HMAC over the workspace's
creation identity, so nothing secret is written to the database, no Secret is
minted per workspace, and the proxy recomputes it from the same attested
fields. ``owner_id`` is in the message, so a *foreign* owner can never derive
the same value; ``pod_name`` is owner-derived, so a *successor Pod for the same
owner* does — deliberately: that user is authorized for both, and a reconnect
after a recreate should not 401.

code-server's contract (``src/node/util.ts``, ``isCookieValid``): when
``HASHED_PASSWORD`` does not contain ``$argon`` it takes the SHA256 branch and
compares the session cookie to the configured value with a constant-time
``safeCompare``. A hex digest never contains ``$argon``, so the credential is
simply a shared secret presented verbatim — no argon2 dependency, no
derivation ambiguity, and no password that any login form could satisfy, since
nobody can produce a plaintext whose SHA256 equals it. Upstream calls the
hash-as-cookie behaviour a flaw (coder/code-server#7696); here it is the
mechanism, which is why the value must never reach the browser.
"""

import hmac
import hashlib
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# Read by code-server itself (env or config.yaml).
IDE_CREDENTIAL_ENV = "HASHED_PASSWORD"

# The cookie code-server checks on every HTTP request and on the WebSocket
# handshake, so one credential binds both transports.
IDE_CREDENTIAL_COOKIE = "code-server-session"

_ROOT_KEY_ENV = "IDE_CREDENTIAL_KEY"

# Domain separation: this key must never produce a value interchangeable with
# another subsystem's token, even if a deployment points them at one secret.
_DOMAIN = "srw-ide-code-server-v1"

# ASCII unit separator. Namespaces and Pod names are DNS labels, owner ids are
# UUIDs and owner kinds are fixed words, so no field can contain it — the
# message is unambiguous and no field can be shifted into another.
_SEP = "\x1f"

_missing_root_logged = False


def ide_credential_root() -> Optional[bytes]:
    """Return the root key, or ``None`` when the deployment has not set one.

    Fail closed: with no root key there is no credential, callers refuse the
    IDE, and that is exactly the containment that already exists today. An
    unset key must never degrade to an unauthenticated upstream.
    """

    global _missing_root_logged
    raw = os.environ.get(_ROOT_KEY_ENV, "")
    if not raw.strip():
        if not _missing_root_logged:
            _missing_root_logged = True
            logger.warning(
                "%s is unset — the browser IDE stays contained (no workspace "
                "credential can be derived)",
                _ROOT_KEY_ENV,
            )
        return None
    return raw.encode("utf-8")


def ide_credential(
    *,
    namespace: str,
    owner_kind: str,
    owner_id: str,
    pod_name: str,
) -> Optional[str]:
    """Derive the exact code-server credential for one workspace runtime.

    Returns ``None`` when any input is missing or no root key is configured;
    every caller treats that as "no IDE", never as "no credential needed".
    """

    root = ide_credential_root()
    if root is None:
        return None
    fields = (namespace, owner_kind, owner_id, pod_name)
    if not all(isinstance(field, str) and field for field in fields):
        return None
    message = _SEP.join((_DOMAIN, *fields)).encode("utf-8")
    return hmac.new(root, message, hashlib.sha256).hexdigest()


def ide_credential_cookie_header(credential: str) -> str:
    """Render the upstream ``Cookie`` value carrying ``credential``."""

    return f"{IDE_CREDENTIAL_COOKIE}={credential}"
