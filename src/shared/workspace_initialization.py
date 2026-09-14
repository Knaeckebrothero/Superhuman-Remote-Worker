"""Versioned VM workspace initialization input and bounded status receipts.

This module is shared by admission, the VM controller and the guest runner.
Commands remain argv arrays throughout; interpreting a shell is an explicit
choice made by the author of a step.
"""

from copy import deepcopy
import hashlib
import json


VERSION = 1
TIMEOUT_SECONDS = 900
MAX_STEPS = 32
MAX_INPUT_BYTES = 64 * 1024
STATE_DIRECTORY = "/var/lib/srw-workspace-initialization"
REQUEST_PATH = "/etc/srw/workspace-initialization.json"
STATUS_PATH = STATE_DIRECTORY + "/status.json"
RUNNER_PATH = "/usr/local/lib/srw/workspace-initialization.py"
SERVICE_NAME = "srw-workspace-initialize.service"


def initialization_request(steps: object) -> dict:
    """Validate a supported recipe and give the exact command sequence an ID."""
    if not isinstance(steps, list) or len(steps) > MAX_STEPS:
        raise ValueError("Workspace initialization supports at most 32 steps.")
    for step in steps:
        if not isinstance(step, dict) or set(step) != {"command"}:
            raise ValueError("Each initialization step requires a command array.")
        argv = step["command"]
        if (
            not isinstance(argv, list)
            or not argv
            or not isinstance(argv[0], str)
            or not argv[0].strip()
            or any(not isinstance(arg, str) or "\x00" in arg for arg in argv)
        ):
            raise ValueError("Initialization commands require valid argument strings.")
    payload = {"version": VERSION, "steps": deepcopy(steps)}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    if len(encoded) > MAX_INPUT_BYTES:
        raise ValueError("Workspace initialization exceeds the 64 KiB input limit.")
    return {**payload, "revision": hashlib.sha256(encoded).hexdigest()}


def validate_initialization_request(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != {"version", "steps", "revision"}:
        raise ValueError("Invalid workspace initialization request.")
    expected = initialization_request(value["steps"])
    if type(value["version"]) is not int or value != expected:
        raise ValueError("Workspace initialization request revision is invalid.")
    return expected


def initialization_receipt(value: object, *, owner_id: str, revision: str) -> dict:
    """Accept only a bounded receipt for the selected owner and recipe."""
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "ownerId", "revision", "phase", "step", "exitCode"}
        or type(value.get("version")) is not int
        or value["version"] != VERSION
        or value.get("ownerId") != owner_id
        or value.get("revision") != revision
        or value.get("phase") not in {"Running", "Succeeded", "Failed"}
        or type(value.get("step")) is not int
        or not 0 <= value["step"] <= MAX_STEPS
        or (
            value.get("exitCode") is not None
            and (
                type(value["exitCode"]) is not int
                or not -255 <= value["exitCode"] <= 255
            )
        )
        or (value.get("phase") == "Succeeded" and value.get("exitCode") != 0)
        or (value.get("phase") == "Running" and value.get("exitCode") is not None)
        or (value.get("phase") == "Failed" and value.get("exitCode") in (None, 0))
    ):
        raise ValueError("Workspace initialization receipt is invalid or stale.")
    return deepcopy(value)
