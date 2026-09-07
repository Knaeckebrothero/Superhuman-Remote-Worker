#!/usr/bin/env python3
"""Seed a running GreenMail instance for `email` datasource tests.

GreenMail's REST API cannot CREATE non-INBOX folders or APPEND a message into
a chosen folder (it only ingests via SMTP, into INBOX), so seeding happens
over plain IMAP with stdlib ``imaplib`` instead — see
knowledge-base/knowledge/features/email_datasource.md §Testing. The script:

  1. logs in (GreenMail runs with ``-Dgreenmail.auth.disabled``, so any
     user/password pair works and the mailbox is created on first use),
  2. CREATEs the ``AI`` and ``Drafts`` folders, tolerating "already exists",
  3. APPENDs every ``*.eml`` fixture from ``--fixtures-dir`` into ``AI`` —
     the first fixture (sorted by filename) flagged ``\\Seen``, the rest
     left unseen,
  4. prints a per-folder message-count summary.

Re-running is safe: folder creation tolerates duplicates, and a fixture is
skipped when a message with its Message-ID already exists in the target
folder, so the seed converges instead of piling up copies.

Usage — port-forward the GreenMail Service, then seed over localhost:3143:

    kubectl --context=k3d-srw -n srw port-forward svc/srw-greenmail 3143:3143 &
    python scripts/seed_greenmail.py
"""

from __future__ import annotations

import argparse
import email
import imaplib
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "greenmail"

SEED_FOLDERS = ("AI", "Drafts")
TARGET_FOLDER = "AI"


def crlf(raw: bytes) -> bytes:
    """Normalize line endings to CRLF as required for IMAP APPEND literals."""
    return raw.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")


def create_folder(conn: imaplib.IMAP4, folder: str) -> str:
    """CREATE a folder, treating "already exists" as success. Returns a label
    for the summary ("created" / "exists")."""
    status, data = conn.create(folder)
    if status == "OK":
        return "created"
    # GreenMail answers NO with a "folder already exists" style message on
    # duplicate CREATE; any NO here means the folder can't be missing-and-
    # uncreatable, so verify by SELECTing it before giving up.
    status, _ = conn.select(folder, readonly=True)
    if status == "OK":
        conn.close()
        return "exists"
    raise RuntimeError(f"cannot create or select folder {folder!r}: {data}")


def message_id_of(raw: bytes, fallback: str) -> str:
    """Extract the Message-ID header of a fixture (used as idempotency key)."""
    msg = email.message_from_bytes(raw)
    return (msg.get("Message-ID") or fallback).strip()


def already_seeded(conn: imaplib.IMAP4, folder: str, message_id: str) -> bool:
    """True when a message with this Message-ID already exists in folder."""
    status, _ = conn.select(folder, readonly=True)
    if status != "OK":
        return False
    try:
        status, data = conn.search(None, "HEADER", "Message-ID", f'"{message_id}"')
        return status == "OK" and bool(data and data[0].split())
    finally:
        conn.close()


def message_count(conn: imaplib.IMAP4, folder: str) -> int | None:
    status, data = conn.select(folder, readonly=True)
    if status != "OK":
        return None
    conn.close()
    return int(data[0])


def seed(args: argparse.Namespace) -> int:
    fixtures = sorted(Path(args.fixtures_dir).glob("*.eml"))
    if not fixtures:
        print(f"error: no *.eml fixtures found in {args.fixtures_dir}", file=sys.stderr)
        return 1

    print(f"Connecting to imap://{args.host}:{args.imap_port} as {args.user} ...")
    conn = imaplib.IMAP4(args.host, args.imap_port)
    try:
        conn.login(args.user, args.password)

        for folder in SEED_FOLDERS:
            print(f"  folder {folder!r}: {create_folder(conn, folder)}")

        appended = skipped = 0
        for index, fixture in enumerate(fixtures):
            raw = crlf(fixture.read_bytes())
            message_id = message_id_of(raw, fallback=f"<{fixture.name}@fixtures>")
            if already_seeded(conn, TARGET_FOLDER, message_id):
                print(f"  {fixture.name}: already in {TARGET_FOLDER!r}, skipped")
                skipped += 1
                continue
            # First fixture lands read (\Seen), the rest unseen, so the seed
            # data exercises both flag states out of the box.
            flags = r"(\Seen)" if index == 0 else None
            status, data = conn.append(
                TARGET_FOLDER, flags, imaplib.Time2Internaldate(time.time()), raw
            )
            if status != "OK":
                print(f"error: APPEND {fixture.name} failed: {data}", file=sys.stderr)
                return 1
            seen = "seen" if flags else "unseen"
            print(f"  {fixture.name}: appended to {TARGET_FOLDER!r} ({seen})")
            appended += 1

        print("\nSummary:")
        for folder in ("INBOX", *SEED_FOLDERS):
            count = message_count(conn, folder)
            shown = count if count is not None else "unavailable"
            print(f"  {folder}: {shown} message(s)")
        print(f"  fixtures appended: {appended}, skipped (already present): {skipped}")
    finally:
        try:
            conn.logout()
        except (imaplib.IMAP4.error, OSError):
            pass
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--host", default="localhost", help="IMAP host (default: %(default)s)"
    )
    parser.add_argument(
        "--imap-port",
        type=int,
        default=3143,
        help="IMAP port (default: %(default)s — GreenMail's non-TLS IMAP)",
    )
    parser.add_argument(
        "--user",
        default="test@example.com",
        help="IMAP login (default: %(default)s — auth is disabled, anything works)",
    )
    parser.add_argument(
        "--password", default="test", help="IMAP password (default: %(default)s)"
    )
    parser.add_argument(
        "--fixtures-dir",
        type=Path,
        default=DEFAULT_FIXTURES_DIR,
        help="Directory of *.eml fixtures to append (default: %(default)s)",
    )
    args = parser.parse_args()

    try:
        sys.exit(seed(args))
    except (imaplib.IMAP4.error, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        print(
            "hint: is GreenMail running and port-forwarded? "
            "(kubectl -n srw port-forward svc/srw-greenmail 3143:3143)",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
