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

import httpx

# state -> (message shown on stderr, process exit code)
# 75 EX_TEMPFAIL, 77 EX_NOPERM, 69 EX_UNAVAILABLE.
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
    "ended": ("this session has ended", 75),
    "restoring": ("workspace is still starting - reconnect in a moment", 75),
    "never_provisioned": ("this session has no workspace yet", 75),
    "vm_unsupported": ("this workspace is VM-tier - SSH access is not supported", 75),
    # These three are real, currently-written workspace_container statuses
    # (see services.ssh_gateway_targets's module docstring and its
    # STATE_FAILED / STATE_DELETED / STATE_STALE_BINDING comments) -- not
    # hypothetical. That module added dedicated constants for them precisely
    # so a distinct, accurate reason reaches whoever reads the refusal;
    # falling back to "unreachable" or "never_provisioned" here would send
    # them after the wrong problem, undoing that.
    "failed": (
        "workspace failed to provision and cannot be recovered - start a new session",
        75,
    ),
    "deleted": (
        "workspace was deleted - start a new session to get a new workspace",
        75,
    ),
    "stale_binding": (
        "workspace is ready but its SSH access is not valid right now - "
        "reconnect via cockpit, or start a new session if this persists",
        75,
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


async def resolve_target(config, handle: str, fingerprint: str) -> SshTarget:
    """Resolve handle + fingerprint to a live workspace, or raise."""
    url = f"{config.orchestrator_url}/api/internal/ssh-targets/{handle}"
    try:
        response = await _http_get(
            url,
            headers={"X-Internal-Key": config.internal_key},
            params={"fingerprint": fingerprint},
            timeout=10.0,
        )
    except Exception:
        # Fail closed: a control-plane outage must not become an open door or a
        # hang, it must become a refusal the user can read.
        raise TargetUnavailable("unreachable") from None

    if response.status_code == 404:
        raise TargetDenied()
    if response.status_code != 200:
        raise TargetUnavailable("unreachable")

    payload = response.json()
    state = payload.get("state")
    if state != "live":
        raise TargetUnavailable(state if state in REFUSAL_MESSAGES else "unreachable")

    try:
        return SshTarget(
            thread_id=payload["thread_id"],
            user_id=payload["user_id"],
            pod_ip=payload["pod_ip"],
            pod_port=int(payload["pod_port"]),
            host_key_fingerprint=payload["host_key_fingerprint"],
            state="live",
        )
    except (KeyError, TypeError, ValueError) as exc:
        # A "live" state is not proof the rest of the payload is well-formed.
        # A missing field (KeyError) or a pod_port that doesn't int() cleanly
        # (TypeError on None, ValueError on a non-numeric string) must become
        # the same readable refusal as any other bad control-plane response,
        # not an unhandled exception surfacing mid-authentication in the SSH
        # server.
        raise TargetUnavailable("unreachable") from exc
