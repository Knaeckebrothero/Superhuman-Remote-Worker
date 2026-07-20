"""Shared-browser stream broker helpers and WebSocket relay."""

from __future__ import annotations

import asyncio
import json
import logging
import shlex
import struct

from security.auth import resolve_ws_user
from services import resolve_ssh_key_path
from services.browser_stream_config import browser_stream_config
from services.canvas_ssh import (
    PINNED_SSH_TRANSPORT_POOL,
    bound_workspace_generation,
    resolve_remote_workspace_target,
)
from services.ssh_helpers import build_agent_ssh_cmd

T_HELLO, T_FRAME, T_STATE, T_INPUT, T_CONTROL, T_ERROR = 1, 2, 3, 4, 5, 6
MAX_STREAM_FRAME = 8 * 1024 * 1024
STREAM_INFO_TIMEOUT_S = 45.0

logger = logging.getLogger(__name__)


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


def _resolve_target(thread: dict):
    """Resolve the provisioner-attested target for the bound generation."""

    return resolve_remote_workspace_target(
        dict(thread),
        bound_workspace_generation(thread),
    )


async def _get_canvas_record(db, thread_id: str):
    """Load the staged browser identity, failing closed on storage errors."""

    from services.canvas import CanvasService

    try:
        return await CanvasService(db).get(thread_id)
    except Exception:
        logger.warning(
            "Could not load browser Canvas generation for thread %s",
            thread_id,
            exc_info=True,
        )
        return None


def _resolve_key_path() -> str:
    key_path = resolve_ssh_key_path()
    if not key_path:
        raise BrowserStreamUnavailable(503, "workspace SSH key unavailable")
    return key_path


def _open_loopback(**kwargs):
    return PINNED_SSH_TRANSPORT_POOL.open_loopback_connection(**kwargs)


_ACTIVE_VIEWERS: dict[str, int] = {}


def _reserve_viewer(thread_id: str, maximum: int) -> bool:
    """Atomically reserve one slot between event-loop suspension points."""

    current = _ACTIVE_VIEWERS.get(thread_id, 0)
    if current >= maximum:
        return False
    _ACTIVE_VIEWERS[thread_id] = current + 1
    return True


def _release_viewer(thread_id: str) -> None:
    remaining = _ACTIVE_VIEWERS.get(thread_id, 1) - 1
    if remaining <= 0:
        _ACTIVE_VIEWERS.pop(thread_id, None)
    else:
        _ACTIVE_VIEWERS[thread_id] = remaining


async def _close_ws(ws, code: int = 1000, reason: str | None = None) -> None:
    try:
        if reason is None:
            await ws.close(code=code)
        else:
            await ws.close(code=code, reason=reason)
    except Exception:
        pass


async def relay_browser_stream(ws, thread_id: str, *, db) -> None:
    """Relay binary WebSocket frames through a generation-pinned SSH channel.

    WebSocket messages carry ``[1-byte type][payload]``. The workspace TCP
    protocol adds only a four-byte length prefix, which this broker mirrors
    without inspecting browser frame or input payloads.
    """

    config = browser_stream_config()
    if not config.enabled:
        await _close_ws(ws, 4404, "Shared browser is not enabled")
        return

    user = await resolve_ws_user(ws, db)
    if not user:
        await _close_ws(ws, 4401, "Authentication required")
        return
    if not user.get("is_approved"):
        await _close_ws(ws, 4403, "Account pending approval")
        return

    thread = await db.get_thread(thread_id)
    if not thread or str(thread.get("user_id") or "") != str(user.get("id") or ""):
        await _close_ws(ws, 4403, "Thread access denied")
        return
    if not workspace_ready(thread):
        await _close_ws(ws, 4503, "Workspace not ready")
        return
    if not _reserve_viewer(thread_id, config.max_viewers):
        await _close_ws(ws, 4429, "Viewer limit reached")
        return

    accepted = False
    stream_failed = False
    try:
        try:
            info = await exec_stream_info(thread)
        except BrowserStreamUnavailable:
            await _close_ws(ws, 4502, "Browser unreachable")
            return

        # The durable Canvas pointer is the browser identity authority. Missing,
        # non-browser, stale, or unreadable state must never silently attach to
        # whatever generation happens to be live in the workspace.
        record = await _get_canvas_record(db, thread_id)
        staged = getattr(
            getattr(record, "source", None),
            "browser_generation",
            None,
        )
        if staged is None or str(staged) != str(info.get("generation")):
            await _close_ws(ws, 4409, "Browser generation ended")
            return

        try:
            target = _resolve_target(thread)
            key_path = _resolve_key_path()
        except Exception:
            await _close_ws(ws, 4503, "Workspace SSH unavailable")
            return

        async def generation_resolver() -> dict:
            current = await db.get_thread(thread_id)
            return dict(current) if current else {}

        await ws.accept()
        accepted = True
        async with _open_loopback(
            target=target,
            destination_port=config.stream_port,
            key_path=key_path,
            generation_resolver=generation_resolver,
        ) as (reader, writer):
            hello = json.dumps(
                {"token": info["token"], "min_protocol": 1},
                separators=(",", ":"),
            ).encode()
            writer.write(encode_stream_frame(T_HELLO, hello))
            await writer.drain()

            async def ws_to_tcp() -> None:
                while True:
                    message = await ws.receive()
                    if message["type"] == "websocket.disconnect":
                        return
                    data = message.get("bytes")
                    if not data or data[0] not in (T_INPUT, T_CONTROL):
                        continue
                    writer.write(encode_stream_frame(data[0], data[1:]))
                    await writer.drain()

            async def tcp_to_ws() -> None:
                while True:
                    frame_type, payload = await read_stream_frame(reader)
                    await ws.send_bytes(bytes([frame_type]) + payload)

            async def touch_activity() -> None:
                while True:
                    await asyncio.sleep(config.activity_interval_seconds)
                    try:
                        await db.merge_thread_workspace_context(thread_id, {})
                    except Exception:
                        logger.debug(
                            "Could not mark browser viewer activity for thread %s",
                            thread_id,
                            exc_info=True,
                        )

            tasks = [
                asyncio.create_task(ws_to_tcp()),
                asyncio.create_task(tcp_to_ws()),
                asyncio.create_task(touch_activity()),
            ]
            done: set[asyncio.Task] = set()
            try:
                done, _ = await asyncio.wait(
                    tasks,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            stream_failed = any(
                not task.cancelled() and task.exception() is not None for task in done
            )
    except Exception:
        stream_failed = True
        logger.debug(
            "Shared-browser stream ended for thread %s",
            thread_id,
            exc_info=True,
        )
    finally:
        _release_viewer(thread_id)
        if accepted:
            if stream_failed:
                await _close_ws(ws, 4502, "stream ended")
            else:
                await _close_ws(ws)


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
    "relay_browser_stream",
    "ssh_endpoint",
    "workspace_ready",
]
