"""Exact remote retirement for managed-repository credential agents.

Control-plane deletion is not process-zero: a partitioned Kubernetes node or
VM guest can keep running after the API reports an accepted delete or 404. The
terminal owners call this module against a server-attested endpoint and host
key before they delete compute. No credential material enters the command,
logs, or durable state; the command only classifies and retires the private
managed ssh-agent namespace.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
import logging
import secrets

try:
    import asyncssh
except ImportError:  # pragma: no cover - deployment dependency guard
    asyncssh = None  # type: ignore[assignment]

from orchestrator.services import resolve_ssh_key_path
from shared.runtime.core.managed_repository import (
    managed_repository_agent_retirement_command,
    managed_repository_agent_zero_command,
)

logger = logging.getLogger(__name__)

_EMPTY_KNOWN_HOSTS = ((), (), (), (), (), (), ())


if asyncssh is not None:

    class _PinnedWorkspaceSSHClient(asyncssh.SSHClient):
        def __init__(self, expected_fingerprint: str):
            self._expected_fingerprint = expected_fingerprint

        def validate_host_public_key(self, host, addr, port, key):  # noqa: ANN001
            del host, addr, port
            return secrets.compare_digest(
                key.get_fingerprint("sha256"), self._expected_fingerprint
            )


async def retire_managed_repository_processes(
    *,
    host: str,
    port: int,
    host_key_fingerprint: str,
    home_path: str = "/home/agent-host",
    operation: str = "managed repository process retirement",
) -> bool:
    """Retire and independently prove zero on one pinned runtime endpoint."""

    key_path = resolve_ssh_key_path()
    if (
        asyncssh is None
        or not key_path
        or not isinstance(host, str)
        or not host
        or isinstance(port, bool)
        or not isinstance(port, int)
        or not 1 <= port <= 65535
        or not isinstance(host_key_fingerprint, str)
        or not host_key_fingerprint.startswith("SHA256:")
    ):
        logger.warning("%s refused without exact SSH authority", operation)
        return False

    # The whole-workspace retirement form deliberately ends each generated
    # cleanup loop with ``;``. Appending another separator verbatim produces
    # ``done; ; set -eu``, which both bash and dash reject. Normalize only the
    # generated trailing delimiter at this composition boundary.
    retirement = managed_repository_agent_retirement_command(
        home_path=home_path,
        authority_ids=None,
        remove_configs=True,
    ).rstrip()
    command = (
        retirement.rstrip(";")
        + "; "
        + managed_repository_agent_zero_command(home_path=home_path)
    )
    connection = None
    try:
        async with asyncio.timeout(30):
            connection = await asyncssh.connect(
                host,
                port=port,
                username="agent-host",
                client_keys=[str(key_path)],
                known_hosts=_EMPTY_KNOWN_HOSTS,
                client_factory=lambda: _PinnedWorkspaceSSHClient(host_key_fingerprint),
                server_host_key_algs=["ssh-ed25519"],
                connect_timeout=10,
                login_timeout=15,
            )
            result = await connection.run(command, check=False, timeout=20)
        if result.exit_status == 0:
            return True
        logger.warning("%s failed with rc=%d", operation, result.exit_status)
        return False
    except TimeoutError:
        logger.warning("%s timed out", operation)
        return False
    except Exception:
        logger.warning("%s failed", operation, exc_info=True)
        return False
    finally:
        if connection is not None:
            connection.close()
            with suppress(Exception):
                await connection.wait_closed()


__all__ = ["retire_managed_repository_processes"]
