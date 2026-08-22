#!/usr/bin/env python3
"""Mock sudo orchestrator for the NATS and same-cluster HTTP transports.

Examples:
    python mock_orchestrator.py --http 18085 --mode approve
    python mock_orchestrator.py --nats nats://127.0.0.1:4222 --mode deny
"""

import argparse
import asyncio
import json
import signal
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

DEFAULT_TOKEN = "a" * 64


def decide(mode: str, delay: float, data: dict) -> tuple[str, str | None, float | None]:
    command = " ".join(data.get("argv") or [data.get("command", "unknown")])
    print(f"→ sudo request: {command}")
    if mode == "approve":
        return "approved", "auto-approved (mock)", None
    if mode == "deny":
        return "denied", "auto-denied (mock)", None
    if mode == "delay":
        return "pending", None, time.monotonic() + delay
    try:
        answer = input("  Approve? [y/N] ").strip().lower()
    except EOFError:
        return "denied", "stdin closed", None
    if answer in ("y", "yes"):
        return "approved", "approved by operator (mock)", None
    return "denied", "denied by operator (mock)", None


def run_http(port: int, mode: str, delay: float, token: str) -> None:
    requests: dict[str, dict] = {}
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        server_version = "SRWMockOrchestrator/1"

        def log_message(self, format_string, *args):
            print(f"HTTP {self.address_string()}: {format_string % args}")

        def send_json(self, status: int, body: dict, headers: dict | None = None):
            encoded = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(encoded)

        def authorized(self) -> bool:
            if self.headers.get("Authorization") != f"Bearer {token}":
                self.send_json(HTTPStatus.UNAUTHORIZED, {"detail": "invalid token"})
                return False
            if self.headers.get_content_type() != "application/json":
                self.send_json(
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                    {"detail": "Content-Type must be application/json"},
                )
                return False
            return True

        def read_json(self) -> dict | None:
            try:
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError("body is not an object")
                return body
            except (ValueError, json.JSONDecodeError):
                self.send_json(HTTPStatus.UNPROCESSABLE_ENTITY, {"detail": "malformed body"})
                return None

        def do_GET(self):
            if not self.authorized():
                return
            parsed = urlparse(self.path)
            parts = parsed.path.strip("/").split("/")
            if (
                len(parts) != 6
                or parts[:3] != ["api", "internal", "vm"]
                or parts[4] != "sudo"
            ):
                self.send_json(HTTPStatus.NOT_FOUND, {"detail": "not found"})
                return
            entity_id, request_id = parts[3], parts[5]
            with lock:
                record = requests.get(request_id)
            if record is None or record["entity_id"] != entity_id:
                self.send_json(HTTPStatus.NOT_FOUND, {"detail": "unknown request_id"})
                return

            wait = min(max(float(parse_qs(parsed.query).get("wait", ["0"])[0]), 0), 25)
            due = record["due"]
            if record["status"] == "pending" and due is not None:
                time.sleep(min(wait, max(0, due - time.monotonic())))
                if time.monotonic() >= due:
                    with lock:
                        record["status"] = "approved"
                        record["reason"] = f"approved after {delay}s delay (mock)"

            self.send_json(
                HTTPStatus.OK,
                {
                    "request_id": request_id,
                    "status": record["status"],
                    "reason": record["reason"],
                },
            )

    def do_post(self: Handler):
        if not self.authorized():
            return
        parsed = urlparse(self.path)
        parts = parsed.path.strip("/").split("/")
        if len(parts) != 5 or parts[:3] != ["api", "internal", "vm"]:
            self.send_json(HTTPStatus.NOT_FOUND, {"detail": "not found"})
            return
        entity_id, operation = parts[3], parts[4]
        body = self.read_json()
        if body is None:
            return
        if operation in ("register", "heartbeat"):
            self.send_json(HTTPStatus.OK, {"ok": True})
            return
        if operation != "sudo" or not isinstance(body.get("request_id"), str):
            self.send_json(HTTPStatus.UNPROCESSABLE_ENTITY, {"detail": "malformed body"})
            return

        request_id = body["request_id"]
        if self.headers.get("Idempotency-Key") != request_id:
            self.send_json(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                {"detail": "Idempotency-Key must match request_id"},
            )
            return
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
        with lock:
            existing = requests.get(request_id)
            if existing is not None:
                if existing["payload"] != canonical or existing["entity_id"] != entity_id:
                    self.send_json(HTTPStatus.CONFLICT, {"detail": "request_id conflict"})
                    return
                status_code = HTTPStatus.OK
                record = existing
            else:
                status, reason, due = decide(mode, delay, body)
                record = {
                    "entity_id": entity_id,
                    "payload": canonical,
                    "status": status,
                    "reason": reason,
                    "due": due,
                }
                requests[request_id] = record
                status_code = HTTPStatus.CREATED
        self.send_json(
            status_code,
            {
                "request_id": request_id,
                "status": record["status"],
                "reason": record["reason"],
                "expires_at": "2099-01-01T00:00:00Z",
            },
        )

    Handler.do_POST = do_post
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"HTTP mock orchestrator listening on http://127.0.0.1:{port}")
    print(f"Mode: {mode}" + (f" (delay: {delay}s)" if mode == "delay" else ""))

    def stop_server(_signum, _frame):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, stop_server)
    signal.signal(signal.SIGTERM, stop_server)
    try:
        server.serve_forever()
    finally:
        server.server_close()


async def run_nats(nats_url: str, mode: str, delay: float, subject: str):
    import nats

    nc = await nats.connect(nats_url)
    print(f"Connected to NATS at {nats_url}")
    print(f"Subscribed to {subject}")
    print(f"Mode: {mode}" + (f" (delay: {delay}s)" if mode == "delay" else ""))

    async def handler(msg):
        try:
            data = json.loads(msg.data.decode())
        except json.JSONDecodeError:
            print(f"ERROR: Invalid JSON: {msg.data!r}")
            return
        status, reason, _due = decide(mode, delay, data)
        if status == "pending":
            await asyncio.sleep(delay)
            status = "approved"
            reason = f"approved after {delay}s delay (mock)"
        response = {"approved": status == "approved", "reason": reason}
        if msg.reply:
            await nc.publish(msg.reply, json.dumps(response).encode())

    subscription = await nc.subscribe(subject, cb=handler)
    stop = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    await subscription.unsubscribe()
    await nc.drain()


def main():
    parser = argparse.ArgumentParser(description="Mock orchestrator for sudo-gated")
    parser.add_argument("--nats", default="nats://127.0.0.1:4222", help="NATS URL")
    parser.add_argument("--http", type=int, metavar="PORT", help="serve HTTP instead of NATS")
    parser.add_argument(
        "--token", default=DEFAULT_TOKEN, help="HTTP bearer token (default: 64 'a' characters)"
    )
    parser.add_argument(
        "--mode",
        choices=["approve", "deny", "interactive", "delay"],
        default="approve",
    )
    parser.add_argument("--delay", type=float, default=5.0)
    parser.add_argument("--subject", default="sudo.request.>")
    args = parser.parse_args()

    if args.http is not None:
        run_http(args.http, args.mode, args.delay, args.token)
    else:
        try:
            asyncio.run(run_nats(args.nats, args.mode, args.delay, args.subject))
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
