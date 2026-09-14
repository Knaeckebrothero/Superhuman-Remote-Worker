"""Trusted builder entry point; all authored commands execute inside libguestfs."""

import hashlib
import json
import os
from pathlib import Path
import shlex
import signal
import stat
import subprocess
import tempfile

from shared.workspace_preparation import builder_request


def guest_script(steps):
    # Shell quoting is only a transport for argv into the isolated guest. The
    # author must explicitly name a shell to request expansion or pipelines.
    commands = ["#!/bin/sh", "set -eu", "export HOME=/root", "cd /"]
    commands.extend(shlex.join(step["command"]) for step in steps)
    return "\n".join(commands) + "\n"


def prepare(request, *, disk=Path("/disk/disk.img")):
    request = builder_request(request)
    before = disk.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("The preparation disk must be a regular raw image.")
    Path(os.environ.get("LIBGUESTFS_CACHEDIR", "/tmp/guestfs-cache")).mkdir(
        mode=0o700, parents=True, exist_ok=True
    )
    with tempfile.TemporaryDirectory(prefix="srw-prepare-") as temp:
        script = Path(temp) / "prepare.sh"
        script.write_text(guest_script(request["steps"]))
        argv = [
            "virt-customize",
            "--format",
            "raw",
            "-a",
            str(disk),
            "--memsize",
            "2048",
            "--smp",
            "2",
            "--network" if request["networkEnabled"] else "--no-network",
            "--run",
            str(script),
            # virt-customize's guest log contains command text. Do not bake it
            # or its random seed into an artifact shared by fresh workspaces.
            "--delete",
            "/builder.log",
            "--delete",
            "/var/lib/systemd/random-seed",
            "--truncate",
            "/etc/machine-id",
            "--delete",
            "/var/lib/dbus/machine-id",
            "--delete",
            "/var/lib/cloud",
        ]
        process = subprocess.Popen(argv, start_new_session=True)
        try:
            code = process.wait(timeout=3500)
        except BaseException:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            raise
        if code:
            raise RuntimeError("Workspace preparation failed.")
    digest = hashlib.sha256()
    fd = os.open(disk, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        after = os.fstat(stream.fileno())
        if (before.st_dev, before.st_ino, before.st_size) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
        ):
            raise ValueError("Preparation disk identity changed.")
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return {
        "version": 1,
        "buildUid": request["buildUid"],
        "pvcUid": request["pvcUid"],
        "cacheKey": request["cacheKey"],
        "phase": "Succeeded",
        "diskSha256": digest.hexdigest(),
        "diskBytes": after.st_size,
    }


def main():
    termination = Path("/dev/termination-log")
    try:
        payload = Path("/request/request.json").read_bytes()
        if len(payload) > 70 * 1024:
            raise ValueError("Builder input exceeds its limit.")
        result = prepare(json.loads(payload))
    except Exception:
        termination.write_text(json.dumps({"version": 1, "phase": "Failed"}))
        raise SystemExit(1) from None
    termination.write_text(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
