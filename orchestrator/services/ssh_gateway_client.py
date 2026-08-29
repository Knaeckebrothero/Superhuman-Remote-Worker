"""The gateway's only view of platform state.

The gateway holds no database credentials. It presents the fingerprint of the
key the client just authenticated with, and the orchestrator maps that to a
user and decides authorization. Sending a user id instead would be rejected:
this codebase does not treat an internal key plus an asserted identity as
equivalent to an authenticated user.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from services.ssh_handles import is_valid_handle

# state -> (message shown on stderr, process exit code)
# 75 EX_TEMPFAIL    -- genuinely retryable without anything else changing:
#                      suspended, reclaimed, ending, restoring.
# 69 EX_UNAVAILABLE -- broken or gone, not fixed by retrying alone: failed,
#                      deleted, ended, stale_binding, never_provisioned,
#                      unreachable.
# 77 EX_NOPERM      -- policy refusals: denied, vm_unsupported.
# Pinned per state, not just to "some legal code" -- an automated retry
# wrapper keying on the exit code would otherwise loop forever against a
# target that can never become live, or give up on one genuinely worth
# retrying. See test_exit_codes_are_pinned_per_state_not_just_in_the_legal_set.
REFUSAL_MESSAGES: dict[str, tuple[str, int]] = {
    "suspended": (
        "workspace suspended - resume the session in cockpit, then reconnect",
        75,
    ),
    "reclaimed": (
        "workspace was reclaimed while idle - resume the session in cockpit to "
        "restore it from its snapshot, then reconnect",
        75,
    ),
    "ending": ("workspace is shutting down - try again once it has stopped", 75),
    "ended": ("this session has ended", 69),
    "restoring": ("workspace is still starting - reconnect in a moment", 75),
    "never_provisioned": ("this session has no workspace yet", 69),
    "vm_unsupported": ("this workspace is VM-tier - SSH access is not supported", 77),
    # These three are real, currently-written workspace_container statuses
    # (see services.ssh_gateway_targets's module docstring and its
    # STATE_FAILED / STATE_DELETED / STATE_STALE_BINDING comments) -- not
    # hypothetical. That module added dedicated constants for them precisely
    # so a distinct, accurate reason reaches whoever reads the refusal;
    # falling back to "unreachable" or "never_provisioned" here would send
    # them after the wrong problem, undoing that.
    "failed": (
        "workspace failed to provision and cannot be recovered - start a new session",
        69,
    ),
    "deleted": (
        "workspace was deleted - start a new session to get a new workspace",
        69,
    ),
    "stale_binding": (
        "workspace is ready but its SSH access is not valid right now - "
        "reconnect via cockpit, or start a new session if this persists",
        69,
    ),
    "unreachable": ("workspace is unreachable right now", 69),
    "denied": ("no such workspace, or you do not have access to it", 77),
}


@dataclass(frozen=True)
class SshTarget:
    thread_id: str
    user_id: str
    pod_ip: str
    pod_port: int
    host_key_fingerprint: str
    state: str


class TargetDenied(Exception):
    """Unknown handle, unknown key, or not authorized — deliberately one case."""


class TargetUnavailable(Exception):
    def __init__(self, state: str):
        super().__init__(state)
        self.state = state


async def _http_get(url: str, headers: dict, params: dict, timeout: float) -> Any:
    async with httpx.AsyncClient(timeout=timeout) as client:
        return await client.get(url, headers=headers, params=params)


def _is_valid_port(value: Any) -> bool:
    """True if ``value`` is a genuine int (never a bool) in 1..65535.

    ``isinstance(True, int)`` is ``True`` (bool subclasses int) and
    ``int(3.9) == 3`` silently truncates, so a bare ``isinstance(x, int)`` or
    ``int(x)`` conversion alone would let a JSON boolean or a fractional
    number through as a "valid" port. Neither is ever a real port, so both
    are excluded explicitly rather than coerced -- no ``int()`` call happens
    anywhere in this path.
    """
    return (
        isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 65535
    )


def _is_valid_identifier(value: Any) -> bool:
    """True if ``value`` is a non-empty string.

    Used for ``pod_ip`` and ``host_key_fingerprint``: a frozen dataclass
    performs no runtime type validation, so a "live" response with a null,
    empty, or non-string value for either would otherwise construct a
    usable-looking ``SshTarget`` that a caller then dials or pins against
    garbage instead of getting the readable refusal this module promises.
    """
    return isinstance(value, str) and bool(value)


async def resolve_target(config, handle: str, fingerprint: str) -> SshTarget:
    """Resolve handle + fingerprint to a live workspace, or raise."""
    if not is_valid_handle(handle):
        # handle is the SSH USERNAME (services.ssh_handles's own docstring)
        # and is therefore fully attacker-controlled at the point a caller
        # reaches this function. It is interpolated into the URL path below,
        # and httpx normalises ".." path segments when building a request --
        # an unvalidated handle is an authenticated GET-request-forgery
        # primitive carrying config.internal_key into arbitrary orchestrator
        # routes that never run the orchestrator's own is_valid_handle
        # check, because a traversed URL never reaches the ssh-targets route
        # at all. Refusing here, before any URL is built or any request is
        # sent, also preserves the deliberate handle/key/authz
        # indistinguishability: an invalid handle now produces exactly the
        # same TargetDenied as an unknown one.
        raise TargetDenied()

    # Belt-and-braces beyond the is_valid_handle gate above: percent-encode
    # so a handle can never reintroduce a "/" or ".." into the path even if
    # that check's charset were ever loosened.
    url = f"{config.orchestrator_url}/api/internal/ssh-targets/{quote(handle, safe='')}"
    try:
        response = await _http_get(
            url,
            headers={"X-Internal-Key": config.internal_key},
            params={"fingerprint": fingerprint},
            timeout=config.orchestrator_request_timeout,
        )
    except Exception:
        # Fail closed: a control-plane outage must not become an open door or a
        # hang, it must become a refusal the user can read.
        raise TargetUnavailable("unreachable") from None

    if response.status_code == 404:
        raise TargetDenied()
    if response.status_code != 200:
        raise TargetUnavailable("unreachable")

    try:
        payload = response.json()
        state = payload.get("state")
        if state != "live":
            raise TargetUnavailable(
                state if state in REFUSAL_MESSAGES else "unreachable"
            )

        pod_ip = payload["pod_ip"]
        pod_port = payload["pod_port"]
        host_key_fingerprint = payload["host_key_fingerprint"]
        if not (
            _is_valid_identifier(pod_ip)
            and _is_valid_port(pod_port)
            and _is_valid_identifier(host_key_fingerprint)
        ):
            raise TargetUnavailable("unreachable")

        return SshTarget(
            thread_id=payload["thread_id"],
            user_id=payload["user_id"],
            pod_ip=pod_ip,
            pod_port=pod_port,
            host_key_fingerprint=host_key_fingerprint,
            state="live",
        )
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        # A "live" state, parseable JSON, and a dict payload are still not
        # proof the rest of the payload is well-formed. A non-JSON body
        # (json.JSONDecodeError, a ValueError subclass), a non-dict payload
        # (AttributeError from .get()/[] on a list/str/None), a missing
        # field (KeyError), or an unhashable state (TypeError from `state in
        # REFUSAL_MESSAGES`) must all become the same readable refusal as
        # any other bad control-plane response, not an unhandled exception
        # surfacing mid-authentication in the SSH server.
        raise TargetUnavailable("unreachable") from exc
