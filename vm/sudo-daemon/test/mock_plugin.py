#!/usr/bin/env python3
"""Mock sudo approval plugin — simulates the C plugin over Unix socket.

Connects to the sudo-gated daemon's Unix socket and sends a length-prefixed
JSON request mimicking what the real C plugin would send. Prints the response.

Usage:
    python mock_plugin.py                          # Default: sudo apt-get install curl
    python mock_plugin.py "rm -rf /tmp/test"       # Custom command
    python mock_plugin.py --socket /tmp/test.sock   # Custom socket path
    python mock_plugin.py --flood 10               # Send N rapid requests (rate limit test)
"""

import argparse
import json
import socket
import struct
import sys
import time


DEFAULT_SOCKET = "/run/sudo-gated/sudo-gated.sock"


def send_request(sock_path: str, command: str, argv: list[str] | None = None) -> dict:
    """Send an approval request and return the response."""
    if argv is None:
        argv = command.split()

    request = {
        "command": f"/usr/bin/{argv[0]}" if not argv[0].startswith("/") else argv[0],
        "runas_user": "root",
        "user": "agent-host",
        "host": "agent-vm-test",
        "tty": "",
        "cwd": "/home/agent-host/workspace",
        "argv": argv,
    }

    payload = json.dumps(request).encode("utf-8")

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(sock_path)
        # Send length prefix (4 bytes big-endian) + JSON payload.
        sock.sendall(struct.pack(">I", len(payload)) + payload)

        print(f"→ Sent request: {request['command']} {' '.join(argv[1:])}")
        print(f"  Waiting for response...")

        # Read 4-byte length prefix.
        length_data = b""
        while len(length_data) < 4:
            chunk = sock.recv(4 - len(length_data))
            if not chunk:
                raise ConnectionError("Connection closed while reading length prefix")
            length_data += chunk

        resp_length = struct.unpack(">I", length_data)[0]

        # Read JSON payload.
        resp_data = b""
        while len(resp_data) < resp_length:
            chunk = sock.recv(resp_length - len(resp_data))
            if not chunk:
                raise ConnectionError("Connection closed while reading response")
            resp_data += chunk

        response = json.loads(resp_data.decode("utf-8"))
        return response

    finally:
        sock.close()


def main():
    parser = argparse.ArgumentParser(description="Mock sudo approval plugin")
    parser.add_argument("command", nargs="?", default="apt-get install -y curl",
                        help="Command to request approval for")
    parser.add_argument("--socket", default=DEFAULT_SOCKET,
                        help=f"Unix socket path (default: {DEFAULT_SOCKET})")
    parser.add_argument("--flood", type=int, default=0,
                        help="Send N rapid requests to test rate limiting")
    args = parser.parse_args()

    if args.flood > 0:
        print(f"Flooding with {args.flood} requests...")
        for i in range(args.flood):
            try:
                resp = send_request(args.socket, args.command)
                status = "APPROVED" if resp.get("approved") else "DENIED"
                reason = resp.get("reason", "")
                print(f"  [{i+1}/{args.flood}] {status} {reason}")
            except Exception as e:
                print(f"  [{i+1}/{args.flood}] ERROR: {e}")
            time.sleep(0.1)
        return

    try:
        response = send_request(args.socket, args.command)
    except FileNotFoundError:
        print(f"ERROR: Socket not found at {args.socket}", file=sys.stderr)
        print(f"Is sudo-gated running?", file=sys.stderr)
        sys.exit(1)
    except ConnectionRefusedError:
        print(f"ERROR: Connection refused at {args.socket}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    if response.get("approved"):
        print(f"← APPROVED")
    else:
        reason = response.get("reason", "no reason given")
        print(f"← DENIED: {reason}")
        sys.exit(1)


if __name__ == "__main__":
    main()
