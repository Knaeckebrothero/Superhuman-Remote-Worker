#!/usr/bin/env python3
"""Conformance check for browser-exec's shared-browser stream mode.

Runs inside the workspace image against the real daemon and Chromium.
Exit zero proves stream identity/authentication, live screencast frames,
baton refusal, viewer control navigation, input dispatch, and baton release.
"""

import asyncio
import importlib.machinery
import importlib.util
import json
import os
import socket
import subprocess
import sys
import time

os.environ.setdefault("BROWSER_EXEC_SOCKET", "/tmp/bx-check.sock")
os.environ.setdefault("BROWSER_EXEC_PROFILE", "/tmp/bx-check-profile")
os.environ.setdefault("BROWSER_EXEC_LOG", "/tmp/bx-check.log")
os.environ.setdefault("BROWSER_EXEC_STREAM_PORT", "38801")

BROWSER_EXEC = os.environ.get("BROWSER_EXEC_BIN", "/usr/local/bin/browser-exec")

loader = importlib.machinery.SourceFileLoader("browser_exec_mod", BROWSER_EXEC)
spec = importlib.util.spec_from_loader("browser_exec_mod", loader)
BE = importlib.util.module_from_spec(spec)
loader.exec_module(BE)

PAGE = "/tmp/bx-check-page.html"


def unix_action(action: str, args: dict, timeout: float = 120.0) -> dict:
    request = json.dumps({"action": action, "args": args}).encode() + b"\n"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        client.connect(os.environ["BROWSER_EXEC_SOCKET"])
        client.sendall(request)
        client.shutdown(socket.SHUT_WR)
        response = b""
        while not response.endswith(b"\n"):
            chunk = client.recv(65536)
            if not chunk:
                break
            response += chunk
    return json.loads(response.decode())


def check(label: str, ok: bool, detail: str = "") -> None:
    status = "ok      " if ok else "FAILED  "
    suffix = f"  {detail}" if detail else ""
    print(f"  {status} {label}{suffix}", flush=True)
    if not ok:
        raise RuntimeError(label)


async def expect_frame(reader, wanted_type: int, timeout: float = 20.0):
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"no frame of type {wanted_type}")
        frame_type, payload = await asyncio.wait_for(
            BE.read_stream_frame(reader), timeout=remaining
        )
        if frame_type == wanted_type:
            return payload


async def send_json(writer, frame_type: int, value: dict) -> None:
    writer.write(BE.encode_stream_frame(frame_type, json.dumps(value).encode()))
    await writer.drain()


async def main() -> int:
    print("Shared-browser stream conformance:", flush=True)
    with open(PAGE, "w") as page:
        page.write(
            """
            <html><body><h1 id="t">stream check</h1>
            <script>
            let n = 0;
            setInterval(() => {
              document.getElementById("t").textContent = `stream check ${n++}`;
            }, 100);
            </script></body></html>
            """
        )

    log_handle = open("/tmp/bx-check.log", "ab")
    daemon = subprocess.Popen(
        [sys.executable, BROWSER_EXEC, "serve"],
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    writer = None
    try:
        for _ in range(20):
            if os.path.exists(os.environ["BROWSER_EXEC_SOCKET"]):
                break
            time.sleep(0.5)

        info = unix_action("stream_info", {"initial_baton": "user"})
        check(
            "stream_info returns identity",
            "generation" in info and "token" in info,
            str({key: info.get(key) for key in ("generation", "port", "baton")}),
        )
        check("initial baton honoured", info.get("baton") == "user")
        port = int(info["port"])

        # Wrong token: ERROR then close.
        bad_reader, bad_writer = await asyncio.open_connection("127.0.0.1", port)
        await send_json(
            bad_writer,
            BE.T_HELLO,
            {"token": "nope", "min_protocol": 1},
        )
        frame_type, payload = await asyncio.wait_for(
            BE.read_stream_frame(bad_reader), timeout=5
        )
        check(
            "bad token rejected",
            frame_type == BE.T_ERROR and json.loads(payload)["code"] == "unauthorized",
        )
        check("bad-token connection closed", await bad_reader.read(1) == b"")
        bad_writer.close()
        await bad_writer.wait_closed()

        # Authenticated viewer.
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        await send_json(
            writer,
            BE.T_HELLO,
            {"token": info["token"], "min_protocol": 1},
        )
        state = json.loads(await expect_frame(reader, BE.T_STATE, timeout=5))
        check(
            "STATE on connect",
            state["generation"] == info["generation"] and state["baton"] == "user",
        )

        # Navigate first so the repainting page guarantees enough compositor
        # frames for everyNthFrame=2.
        await send_json(
            writer,
            BE.T_CONTROL,
            {"op": "navigate", "url": f"file://{PAGE}"},
        )
        while True:
            state = json.loads(await expect_frame(reader, BE.T_STATE))
            if "127.0.0.1" in (state.get("url") or ""):
                break
        check(
            "control navigate lands",
            "127.0.0.1" in (state.get("url") or ""),
            state.get("url") or "",
        )

        frame_payload = await expect_frame(reader, BE.T_FRAME)
        header, jpeg = BE.decode_frame_payload(frame_payload)
        check(
            "screencast frame arrives",
            jpeg[:2] == b"\xff\xd8",
            f"{len(jpeg)} bytes",
        )
        check(
            "frame carries generation",
            header["generation"] == info["generation"],
        )

        # Mutating agent actions are refused; reads remain available.
        response = unix_action("navigate", {"url": f"file://{PAGE}"})
        check(
            "agent navigate refused while user drives",
            response.get("error") == "user_is_driving",
        )
        response = unix_action("screenshot", {})
        check(
            "agent screenshot allowed while user drives",
            "screenshot" in response and response.get("baton") == "user",
        )

        # Viewer input dispatch must leave the stream alive.
        await send_json(
            writer,
            BE.T_INPUT,
            {
                "kind": "mouse",
                "params": {
                    "type": "mouseMoved",
                    "x": 5,
                    "y": 5,
                },
            },
        )
        await asyncio.sleep(0.5)

        # Release to the agent and prove mutations resume.
        await send_json(writer, BE.T_CONTROL, {"op": "release_baton"})
        state = json.loads(await expect_frame(reader, BE.T_STATE))
        check("baton released", state["baton"] == "agent")
        response = unix_action("navigate", {"url": f"file://{PAGE}"})
        check(
            "agent navigate allowed after release",
            "error" not in response,
            str(response.get("error", "")),
        )

        writer.close()
        await writer.wait_closed()
        writer = None
        print("Shared-browser stream conformance OK.", flush=True)
        return 0
    finally:
        if writer is not None:
            writer.close()
            await writer.wait_closed()
        try:
            unix_action("shutdown", {}, timeout=10)
        except Exception:
            pass
        try:
            daemon.wait(timeout=10)
        except subprocess.TimeoutExpired:
            daemon.terminate()
            daemon.wait(timeout=10)
        log_handle.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
