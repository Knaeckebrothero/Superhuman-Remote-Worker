#!/usr/bin/env python3
"""Renumber audit step_number values for a job in chronological order.

When an agent process restarts (e.g., --resume), the in-memory step counter
resets to 0, creating duplicate step_number values. This script fixes that by
reassigning sequential step_numbers based on timestamp order.

Usage:
    # Preview changes (default, no writes)
    python scripts/renumber_audit_steps.py --job-id <uuid>

    # Apply changes
    python scripts/renumber_audit_steps.py --job-id <uuid> --apply
"""

import argparse
import os
import sys

from dotenv import load_dotenv
from pymongo import MongoClient, UpdateOne


def connect(mongodb_url: str, db_name: str = "graphrag_logs"):
    """Connect to MongoDB and return the database."""
    client = MongoClient(mongodb_url, serverSelectionTimeoutMS=5000)
    # Verify connectivity
    client.admin.command("ping")
    return client[db_name]


def renumber_collection(collection, job_id: str, *, apply: bool) -> dict:
    """Renumber step_number for all entries in a collection for a given job.

    Sorts by (timestamp ASC, _id ASC) and assigns sequential step_numbers.

    Returns:
        Summary dict with total, changed, old_max, new_max.
    """
    docs = list(
        collection.find(
            {"job_id": job_id},
            projection={"_id": 1, "step_number": 1, "timestamp": 1},
        ).sort([("timestamp", 1), ("_id", 1)])
    )

    if not docs:
        return {"total": 0, "changed": 0, "old_max": 0, "new_max": 0}

    old_max = max(d.get("step_number", 0) for d in docs)

    # Build update operations for docs whose step_number needs to change
    ops = []
    changed = 0
    for i, doc in enumerate(docs):
        new_step = i + 1
        if doc.get("step_number") != new_step:
            ops.append(
                UpdateOne({"_id": doc["_id"]}, {"$set": {"step_number": new_step}})
            )
            changed += 1

    if apply and ops:
        result = collection.bulk_write(ops, ordered=False)
        assert result.modified_count == changed, (
            f"Expected {changed} modifications, got {result.modified_count}"
        )

    return {
        "total": len(docs),
        "changed": changed,
        "old_max": old_max,
        "new_max": len(docs),
    }


def main():
    load_dotenv()

    parser = argparse.ArgumentParser(
        description="Renumber audit step_number values for a job."
    )
    parser.add_argument("--job-id", required=True, help="Job UUID to renumber")
    parser.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Apply changes (default is dry-run)",
    )
    args = parser.parse_args()

    mongodb_url = os.getenv("MONGODB_URL")
    if not mongodb_url:
        print("Error: MONGODB_URL not set in environment or .env", file=sys.stderr)
        sys.exit(1)

    db = connect(mongodb_url)
    mode = "APPLY" if args.apply else "DRY RUN"

    for coll_name in ("agent_audit", "chat_history"):
        collection = db[coll_name]
        stats = renumber_collection(collection, args.job_id, apply=args.apply)

        if stats["total"] == 0:
            print(f"[{coll_name}] No entries found for job {args.job_id}")
            continue

        print(f"[{coll_name}] [{mode}]")
        print(f"  Total entries:  {stats['total']}")
        print(f"  Need renumber:  {stats['changed']}")
        print(f"  Old max step#:  {stats['old_max']}")
        print(f"  New max step#:  {stats['new_max']}")

    if not args.apply:
        print("\nNo changes written. Pass --apply to execute.")


if __name__ == "__main__":
    main()
