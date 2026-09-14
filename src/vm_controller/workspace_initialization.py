"""Deliver initialization into a VM, independently of its agent harness."""

import base64
from importlib.resources import files
import json
from uuid import UUID

import yaml

from shared.workspace_initialization import (
    REQUEST_PATH,
    RUNNER_PATH,
    SERVICE_NAME,
    TIMEOUT_SECONDS,
    validate_initialization_request,
)


def inject_workspace_initialization(
    cloud_init: str, *, owner_id: str, request: dict
) -> str:
    recipe = validate_initialization_request(request)
    owner_id = str(UUID(owner_id))
    configuration = yaml.safe_load(cloud_init)
    if not isinstance(configuration, dict):
        raise ValueError("VM initialization requires a cloud-config mapping.")
    unit = (
        "[Unit]\nDescription=Initialize the SRW workspace\n"
        "After=network-online.target\nWants=network-online.target\n"
        "[Service]\nType=exec\nUser=root\n"
        f"ExecStart=/usr/bin/python3 -E {RUNNER_PATH}\n"
        f"RuntimeMaxSec={TIMEOUT_SECONDS + 30}s\nTimeoutStopSec=10s\n"
        "KillMode=control-group\nRestart=no\n"
        "[Install]\nWantedBy=multi-user.target\n"
    )
    payloads = {
        REQUEST_PATH: (json.dumps({"ownerId": owner_id, "recipe": recipe}), "0600"),
        RUNNER_PATH: (
            files("vm_controller").joinpath("guest_initialization.py").read_text(),
            "0644",
        ),
        "/usr/local/lib/srw/shared/__init__.py": ("", "0644"),
        "/usr/local/lib/srw/shared/workspace_initialization.py": (
            files("shared").joinpath("workspace_initialization.py").read_text(),
            "0644",
        ),
        f"/etc/systemd/system/{SERVICE_NAME}": (unit, "0644"),
    }
    existing = configuration.setdefault("write_files", [])
    if not isinstance(existing, list) or any(
        not isinstance(item, dict) or item.get("path") in payloads for item in existing
    ):
        raise ValueError(
            "VM initialization paths conflict with the cloud-init template."
        )
    for path, (content, permissions) in payloads.items():
        existing.append(
            {
                "path": path,
                "owner": "root:root",
                "permissions": permissions,
                "encoding": "b64",
                "content": base64.b64encode(content.encode()).decode("ascii"),
            }
        )
    commands = configuration.setdefault("runcmd", [])
    if not isinstance(commands, list):
        raise ValueError("VM initialization requires cloud-init runcmd ordering.")
    commands.extend(
        [
            ["systemctl", "daemon-reload"],
            ["systemctl", "enable", SERVICE_NAME],
            ["systemctl", "start", "--no-block", SERVICE_NAME],
        ]
    )
    return "#cloud-config\n" + yaml.safe_dump(configuration, sort_keys=False)
