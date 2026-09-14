"""Guest-only initialization runner, delivered by the VM's cloud-init Secret.

The controller renders this program; it never executes it. systemd owns each
unprivileged step and its descendants. The root-owned receipt survives VM
replacement when the original rootdisk is retained.
"""

import fcntl
import json
import os
from pathlib import Path
import pwd
import subprocess
import time
from uuid import UUID

from shared.workspace_initialization import (
    REQUEST_PATH,
    SERVICE_NAME,
    STATE_DIRECTORY,
    TIMEOUT_SECONDS,
    VERSION,
    initialization_receipt,
    validate_initialization_request,
)


def step_command(
    owner_id: str, index: int, argv: list[str], remaining: int
) -> list[str]:
    return [
        "systemd-run",
        "--quiet",
        "--wait",
        "--collect",
        "--expand-environment=no",
        "--service-type=exec",
        f"--unit=srw-initialize-{owner_id}-{index}",
        "--description=SRW workspace initialization step",
        "--property=User=agent-host",
        "--property=WorkingDirectory=/home/agent-host/workspace",
        "--property=Environment=HOME=/home/agent-host USER=agent-host LOGNAME=agent-host",
        "--property=NoNewPrivileges=yes",
        "--property=KillMode=control-group",
        "--property=TimeoutStopSec=5s",
        f"--property=RuntimeMaxSec={max(1, remaining)}s",
        f"--property=BindsTo={SERVICE_NAME}",
        "--property=StandardInput=null",
        "--property=StandardOutput=append:/var/log/srw-workspace-initialization.log",
        "--property=StandardError=append:/var/log/srw-workspace-initialization.log",
        "--",
        *argv,
    ]


def _write_receipt(directory: Path, receipt: dict) -> None:
    temporary = directory / "status.json.tmp"
    with temporary.open("w") as target:
        os.chmod(temporary, 0o644)
        json.dump(receipt, target, sort_keys=True)
        target.flush()
        os.fsync(target.fileno())
    temporary.replace(directory / "status.json")
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def run_initialization() -> int:
    if os.geteuid() != 0:
        return 77
    request = json.loads(Path(REQUEST_PATH).read_text())
    owner_id = str(UUID(request["ownerId"]))
    if request["ownerId"] != owner_id:
        return 78
    recipe = validate_initialization_request(request["recipe"])
    directory = Path(STATE_DIRECTORY)
    directory.mkdir(mode=0o755, parents=True, exist_ok=True)
    if directory.is_symlink() or directory.stat().st_uid != 0:
        return 78
    os.chmod(directory, 0o755)
    lock_fd = os.open(directory / "lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 75
        status_path = directory / "status.json"
        previous = None
        if status_path.exists():
            previous = initialization_receipt(
                json.loads(status_path.read_text()),
                owner_id=owner_id,
                revision=recipe["revision"],
            )
            if previous["step"] > len(recipe["steps"]) or (
                previous["phase"] == "Failed"
                and previous["step"] >= len(recipe["steps"])
            ):
                return 78
            if previous["phase"] == "Succeeded":
                return 0 if previous["step"] == len(recipe["steps"]) else 78
        user = pwd.getpwnam("agent-host")
        workspace = Path("/home/agent-host/workspace")
        if not workspace.exists():
            workspace.mkdir(mode=0o755, parents=True)
            os.chown(workspace, user.pw_uid, user.pw_gid)
        # Do not send command output into cloud-init or the orchestrator logs.
        log_fd = os.open(
            "/var/log/srw-workspace-initialization.log",
            os.O_CREAT | os.O_APPEND | os.O_WRONLY | os.O_NOFOLLOW,
            0o600,
        )
        os.fchmod(log_fd, 0o600)
        os.close(log_fd)
        start = previous["step"] if previous is not None else 0
        receipt = {
            "version": VERSION,
            "ownerId": owner_id,
            "revision": recipe["revision"],
            "phase": "Running",
            "step": start,
            "exitCode": None,
        }
        _write_receipt(directory, receipt)
        deadline = time.monotonic() + TIMEOUT_SECONDS
        for index in range(start, len(recipe["steps"])):
            remaining = int(deadline - time.monotonic())
            if remaining <= 0:
                code = 124
            else:
                try:
                    result = subprocess.run(
                        step_command(
                            owner_id,
                            index,
                            recipe["steps"][index]["command"],
                            remaining,
                        ),
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=False,
                    )
                    code = result.returncode
                except OSError:
                    code = 127
            if code != 0:
                receipt.update(phase="Failed", step=index, exitCode=code)
                _write_receipt(directory, receipt)
                return 1
            receipt["step"] = index + 1
            _write_receipt(directory, receipt)
        receipt.update(phase="Succeeded", exitCode=0)
        _write_receipt(directory, receipt)
        return 0
    finally:
        os.close(lock_fd)


if __name__ == "__main__":
    try:
        result = run_initialization()
    except Exception:
        # Inputs and guest paths can contain private data. Missing/malformed
        # state remains non-ready; the operator can inspect the guest locally.
        result = 78
    raise SystemExit(result)
