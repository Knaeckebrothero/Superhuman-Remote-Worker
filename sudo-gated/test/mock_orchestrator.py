#!/usr/bin/env python3
"""Mock orchestrator — NATS subscriber that auto-approves/denies sudo requests.

Subscribes to sudo.request.> and responds to each request based on the
configured mode. Used for testing the daemon without the real orchestrator.

Usage:
    python mock_orchestrator.py                         # Auto-approve everything
    python mock_orchestrator.py --mode deny             # Auto-deny everything
    python mock_orchestrator.py --mode interactive      # Ask for each request
    python mock_orchestrator.py --mode delay --delay 5  # Approve after 5s delay
    python mock_orchestrator.py --nats nats://10.0.50.10:4222
"""

import argparse
import asyncio
import json
import signal
import sys

import nats


async def run(nats_url: str, mode: str, delay: float, subject: str):
    nc = await nats.connect(nats_url)
    print(f"Connected to NATS at {nats_url}")
    print(f"Subscribed to {subject}")
    print(f"Mode: {mode}" + (f" (delay: {delay}s)" if mode == "delay" else ""))
    print("---")

    async def handler(msg):
        try:
            data = json.loads(msg.data.decode())
        except json.JSONDecodeError:
            print(f"ERROR: Invalid JSON: {msg.data!r}")
            return

        command = data.get("command", "unknown")
        argv = data.get("argv", [])
        user = data.get("user", "unknown")
        vm_id = data.get("vm_id", "unknown")
        job_id = data.get("job_id", "unknown")

        cmd_str = " ".join(argv) if argv else command
        print(f"\n→ Request from {vm_id} (job {job_id})")
        print(f"  User: {user} → {data.get('runas_user', 'root')}")
        print(f"  Command: {cmd_str}")
        print(f"  CWD: {data.get('cwd', 'unknown')}")

        approved = False
        reason = ""

        if mode == "approve":
            approved = True
            reason = "auto-approved (mock)"
        elif mode == "deny":
            approved = False
            reason = "auto-denied (mock)"
        elif mode == "delay":
            print(f"  Waiting {delay}s before approving...")
            await asyncio.sleep(delay)
            approved = True
            reason = f"approved after {delay}s delay (mock)"
        elif mode == "interactive":
            try:
                answer = input(f"  Approve? [y/N] ").strip().lower()
                approved = answer in ("y", "yes")
                if not approved:
                    reason = input(f"  Denial reason: ").strip() or "denied by operator (mock)"
                else:
                    reason = "approved by operator (mock)"
            except EOFError:
                approved = False
                reason = "stdin closed"

        response = {"approved": approved}
        if reason:
            response["reason"] = reason

        status = "APPROVED" if approved else "DENIED"
        print(f"← {status}: {reason}")

        if msg.reply:
            await nc.publish(msg.reply, json.dumps(response).encode())
        else:
            print("  WARNING: No reply subject — cannot respond")

    sub = await nc.subscribe(subject, cb=handler)

    # Wait for shutdown signal.
    stop = asyncio.Event()

    def signal_handler():
        print("\nShutting down...")
        stop.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, signal_handler)

    await stop.wait()
    await sub.unsubscribe()
    await nc.drain()
    print("Disconnected from NATS")


def main():
    parser = argparse.ArgumentParser(description="Mock orchestrator for sudo-gated")
    parser.add_argument("--nats", default="nats://127.0.0.1:4222",
                        help="NATS URL")
    parser.add_argument("--mode", choices=["approve", "deny", "interactive", "delay"],
                        default="approve",
                        help="Response mode (default: approve)")
    parser.add_argument("--delay", type=float, default=5.0,
                        help="Delay in seconds for 'delay' mode (default: 5)")
    parser.add_argument("--subject", default="sudo.request.>",
                        help="NATS subject to subscribe to (default: sudo.request.>)")
    args = parser.parse_args()

    try:
        asyncio.run(run(args.nats, args.mode, args.delay, args.subject))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
