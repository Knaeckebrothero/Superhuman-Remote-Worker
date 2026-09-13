"""Read guest initialization results before publishing workspace readiness."""

import asyncio
import json

from orchestrator.services import resolve_ssh_key_path
from orchestrator.services.blocking_effect import joined_async_call
from orchestrator.services.ssh_helpers import pinned_agent_ssh_command
from orchestrator.services.subprocess_effect import (
    communicate_bounded,
    create_owned_subprocess_exec,
    stop_and_reap,
)
from shared.workspace_initialization import (
    STATUS_PATH,
    initialization_receipt,
    validate_initialization_request,
)


async def read_vm_initialization(
    attestation, *, owner_id: str, request: dict
) -> dict | None:
    """A missing, malformed or unreadable receipt is never readiness evidence."""
    expected = validate_initialization_request(request)
    # The command and path are platform constants. Bound the remote read as
    # well as local output; a receipt contains no command text or credentials.
    command = f"head -c 4097 -- {STATUS_PATH}"
    async with pinned_agent_ssh_command(
        attestation.host,
        attestation.port,
        command,
        key_path=resolve_ssh_key_path(),
        expected_host_key_fingerprint=attestation.ssh_host_key_fingerprint,
    ) as argv:
        process = await create_owned_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            output, _error = await joined_async_call(
                communicate_bounded(
                    process, timeout=10, stdout_limit=4097, stderr_limit=1024
                )
            )
        except BaseException:
            if process.returncode is None:
                await stop_and_reap(process)
            raise
    if process.returncode != 0 or len(output) > 4096:
        return None
    try:
        receipt = initialization_receipt(
            json.loads(output), owner_id=owner_id, revision=expected["revision"]
        )
    except (ValueError, TypeError):
        return None
    if (
        receipt["step"] > len(expected["steps"])
        or (
            receipt["phase"] == "Succeeded"
            and receipt["step"] != len(expected["steps"])
        )
        or (receipt["phase"] == "Failed" and receipt["step"] >= len(expected["steps"]))
    ):
        return None
    return receipt
