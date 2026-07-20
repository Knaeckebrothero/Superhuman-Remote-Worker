"""Shared-browser stream broker helpers and WebSocket relay."""

from __future__ import annotations

import asyncio
import json
import shlex
import struct

from services.browser_stream_config import browser_stream_config
from services.ssh_helpers import build_agent_ssh_cmd

T_HELLO, T_FRAME, T_STATE, T_INPUT, T_CONTROL, T_ERROR = 1, 2, 3, 4, 5, 6
MAX_STREAM_FRAME = 8 * 1024 * 1024
STREAM_INFO_TIMEOUT_S = 45.0


class BrowserStreamUnavailable(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def encode_stream_frame(frame_type: int, payload: bytes) -> bytes:
    if len(payload) + 1 > MAX_STREAM_FRAME:
        raise ValueError(f"stream frame too large: {len(payload) + 1}")
    return struct.pack(">IB", len(payload) + 1, frame_type) + payload


async def read_stream_frame(reader) -> tuple[int, bytes]:
    head = await reader.readexactly(4)
    (length,) = struct.unpack(">I", head)
    if not 1 <= length <= MAX_STREAM_FRAME:
        raise ValueError(f"bad stream frame length: {length}")
    body = await reader.readexactly(length)
    return body[0], body[1:]


def _metadata(thread: dict) -> dict:
    value = thread.get("metadata") or {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            value = {}
    return value if isinstance(value, dict) else {}


def _active_context(thread: dict) -> dict:
    """Prefer a ready VM, otherwise use the container workspace context."""
    metadata = _metadata(thread)
    vm = metadata.get("vm") or {}
    if isinstance(vm, dict) and vm.get("status") == "ready":
        return vm
    container = metadata.get("workspace_container") or {}
    return container if isinstance(container, dict) else {}


def workspace_ready(thread: dict) -> bool:
    return _active_context(thread).get("status") == "ready"


def ssh_endpoint(thread: dict) -> tuple[str, int]:
    context = _active_context(thread)
    if context.get("status") != "ready":
        raise BrowserStreamUnavailable(503, "workspace is not ready")
    host = context.get("ssh_host") or context.get("host") or context.get("pod_ip")
    if (
        not isinstance(host, str)
        or not host
        or any(char.isspace() or ord(char) < 33 for char in host)
    ):
        raise BrowserStreamUnavailable(503, "workspace SSH endpoint unavailable")
    try:
        port = int(context.get("ssh_port") or context.get("port") or 22)
    except (TypeError, ValueError) as exc:
        raise BrowserStreamUnavailable(
            503, "workspace SSH endpoint unavailable"
        ) from exc
    if not 1 <= port <= 65535:
        raise BrowserStreamUnavailable(503, "workspace SSH endpoint unavailable")
    return host, port


async def exec_stream_info(thread: dict, *, initial_baton: str | None = None) -> dict:
    """Cold-start browser-exec over SSH and return stream identity."""
    host, port = ssh_endpoint(thread)
    config = browser_stream_config()
    args: dict = {}
    if initial_baton:
        args["initial_baton"] = initial_baton
    encoded_args = json.dumps(args, separators=(",", ":"))
    remote = f"browser-exec stream_info --json {shlex.quote(encoded_args)}"
    command = build_agent_ssh_cmd(
        host,
        port,
        remote,
        connect_timeout_s=config.connect_timeout_seconds,
        batch_mode=True,
    )
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise BrowserStreamUnavailable(
            502, "browser-exec SSH command could not start"
        ) from exc
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=STREAM_INFO_TIMEOUT_S
        )
    except asyncio.TimeoutError as exc:
        process.kill()
        raise BrowserStreamUnavailable(504, "browser start timed out") from exc
    if process.returncode != 0:
        tail = stderr.decode(errors="replace")[-300:]
        raise BrowserStreamUnavailable(502, f"browser-exec unreachable: {tail}")
    lines = [
        line for line in stdout.decode(errors="replace").splitlines() if line.strip()
    ]
    if not lines:
        raise BrowserStreamUnavailable(502, "browser-exec returned no output")
    try:
        info = json.loads(lines[-1])
    except ValueError as exc:
        raise BrowserStreamUnavailable(
            502, "browser-exec returned malformed output"
        ) from exc
    if not isinstance(info, dict):
        raise BrowserStreamUnavailable(502, "browser-exec returned malformed output")
    if "error" in info:
        raise BrowserStreamUnavailable(502, str(info["error"]))
    try:
        stream_port = int(info["port"])
    except (KeyError, TypeError, ValueError) as exc:
        raise BrowserStreamUnavailable(
            502, "browser-exec returned incomplete stream identity"
        ) from exc
    if (
        not isinstance(info.get("generation"), str)
        or not info["generation"]
        or not isinstance(info.get("token"), str)
        or not info["token"]
        or info.get("baton") not in {"agent", "user"}
        or stream_port != config.stream_port
    ):
        raise BrowserStreamUnavailable(
            502, "browser-exec returned invalid stream identity"
        )
    return info


__all__ = [
    "BrowserStreamUnavailable",
    "MAX_STREAM_FRAME",
    "T_CONTROL",
    "T_ERROR",
    "T_FRAME",
    "T_HELLO",
    "T_INPUT",
    "T_STATE",
    "encode_stream_frame",
    "exec_stream_info",
    "read_stream_frame",
    "ssh_endpoint",
    "workspace_ready",
]
