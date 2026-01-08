#!/usr/bin/env python3
"""View LLM conversation history from MongoDB.

This script queries the MongoDB archive to display LLM request/response
history for debugging and analysis.

Usage:
    # View all requests for a job
    python scripts/view_llm_conversation.py --job-id <uuid>

    # View recent requests across all jobs
    python scripts/view_llm_conversation.py --recent 20

    # Get statistics for a job
    python scripts/view_llm_conversation.py --job-id <uuid> --stats

    # Export conversation to JSON
    python scripts/view_llm_conversation.py --job-id <uuid> --export conv.json

    # Show only tool calls
    python scripts/view_llm_conversation.py --job-id <uuid> --tools-only
"""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv()


def get_mongodb_client():
    """Get MongoDB client."""
    from pymongo import MongoClient

    url = os.getenv("MONGODB_URL", "mongodb://localhost:27017/graphrag_logs")
    client = MongoClient(url, serverSelectionTimeoutMS=5000)

    # Extract database name
    db_name = url.split("/")[-1].split("?")[0] or "graphrag_logs"

    return client, db_name


def format_timestamp(ts):
    """Format timestamp for display."""
    if isinstance(ts, datetime):
        return ts.strftime("%Y-%m-%d %H:%M:%S")
    return str(ts)


def truncate(text, max_len=200):
    """Truncate text with ellipsis."""
    if not text:
        return ""
    text = str(text)
    if len(text) <= max_len:
        return text
    return text[:max_len] + "..."


def print_message(msg, indent=4):
    """Print a message with formatting."""
    prefix = " " * indent
    role = msg.get("role", msg.get("type", "unknown"))
    content = msg.get("content", "")

    # Color coding (ANSI)
    colors = {
        "system": "\033[36m",  # Cyan
        "human": "\033[32m",    # Green
        "assistant": "\033[33m", # Yellow
        "tool": "\033[35m",      # Magenta
    }
    reset = "\033[0m"
    color = colors.get(role, "")

    print(f"{prefix}{color}[{role}]{reset} {truncate(content, 100)}")

    # Print tool calls if present
    if msg.get("tool_calls"):
        for tc in msg["tool_calls"]:
            print(f"{prefix}  -> Tool: {tc.get('name')} (id: {tc.get('id', '?')[:8]})")


def view_conversation(job_id, limit=100, tools_only=False, full=False):
    """View conversation for a job."""
    client, db_name = get_mongodb_client()
    db = client[db_name]
    collection = db["llm_requests"]

    # Query
    cursor = collection.find({"job_id": job_id}).sort("timestamp", 1).limit(limit)
    records = list(cursor)

    if not records:
        print(f"No records found for job: {job_id}")
        return

    print(f"\n{'=' * 70}")
    print(f"Conversation History: {job_id}")
    print(f"Found {len(records)} LLM requests")
    print(f"{'=' * 70}\n")

    for i, record in enumerate(records):
        ts = format_timestamp(record.get("timestamp"))
        iteration = record.get("iteration", "?")
        latency = record.get("latency_ms", "?")
        model = record.get("model", "?")

        print(f"[{i+1}] Iteration {iteration} | {ts} | {latency}ms | {model}")
        print("-" * 60)

        # Request messages
        request = record.get("request", {})
        messages = request.get("messages", [])

        if not tools_only:
            print("  Request:")
            for msg in messages[-5:]:  # Show last 5 messages
                print_message(msg)

        # Response
        response = record.get("response", {})
        print("  Response:")
        print_message(response)

        # Metrics
        metrics = record.get("metrics", {})
        if metrics:
            print(f"  Metrics: input={metrics.get('input_chars', '?')} chars, "
                  f"output={metrics.get('output_chars', '?')} chars, "
                  f"tools={metrics.get('tool_calls', 0)}")

        print()

    client.close()


def view_recent(limit=20, agent_type=None):
    """View recent requests across all jobs."""
    client, db_name = get_mongodb_client()
    db = client[db_name]
    collection = db["llm_requests"]

    query = {}
    if agent_type:
        query["agent_type"] = agent_type

    cursor = collection.find(query).sort("timestamp", -1).limit(limit)
    records = list(cursor)

    if not records:
        print("No records found")
        return

    print(f"\n{'=' * 70}")
    print(f"Recent LLM Requests")
    print(f"{'=' * 70}\n")

    for record in records:
        ts = format_timestamp(record.get("timestamp"))
        job_id = record.get("job_id", "?")[:8]
        agent = record.get("agent_type", "?")
        iteration = record.get("iteration", "?")
        latency = record.get("latency_ms", "?")

        response = record.get("response", {})
        tool_calls = len(response.get("tool_calls", []))
        content_len = len(response.get("content", ""))

        print(f"{ts} | job={job_id}... | {agent:10} | iter={iteration:3} | "
              f"{latency:5}ms | {content_len:5} chars | {tool_calls} tools")

    client.close()


def get_job_stats(job_id):
    """Get statistics for a job."""
    client, db_name = get_mongodb_client()
    db = client[db_name]
    collection = db["llm_requests"]

    pipeline = [
        {"$match": {"job_id": job_id}},
        {
            "$group": {
                "_id": "$job_id",
                "total_requests": {"$sum": 1},
                "total_input_chars": {"$sum": "$metrics.input_chars"},
                "total_output_chars": {"$sum": "$metrics.output_chars"},
                "total_tool_calls": {"$sum": "$metrics.tool_calls"},
                "avg_latency_ms": {"$avg": "$latency_ms"},
                "max_latency_ms": {"$max": "$latency_ms"},
                "min_latency_ms": {"$min": "$latency_ms"},
                "first_request": {"$min": "$timestamp"},
                "last_request": {"$max": "$timestamp"},
                "models_used": {"$addToSet": "$model"},
            }
        },
    ]

    results = list(collection.aggregate(pipeline))

    if not results:
        print(f"No records found for job: {job_id}")
        return

    stats = results[0]

    print(f"\n{'=' * 70}")
    print(f"Job Statistics: {job_id}")
    print(f"{'=' * 70}\n")

    # Calculate duration
    first = stats.get("first_request")
    last = stats.get("last_request")
    if first and last:
        duration = (last - first).total_seconds()
    else:
        duration = 0

    print(f"Total Requests:     {stats.get('total_requests', 0)}")
    print(f"Duration:           {duration:.1f}s")
    print(f"Models Used:        {', '.join(stats.get('models_used', []))}")
    print()
    print(f"Total Input Chars:  {stats.get('total_input_chars', 0):,}")
    print(f"Total Output Chars: {stats.get('total_output_chars', 0):,}")
    print(f"Total Tool Calls:   {stats.get('total_tool_calls', 0)}")
    print()
    print(f"Avg Latency:        {stats.get('avg_latency_ms', 0):.0f}ms")
    print(f"Min Latency:        {stats.get('min_latency_ms', 0):.0f}ms")
    print(f"Max Latency:        {stats.get('max_latency_ms', 0):.0f}ms")
    print()
    print(f"First Request:      {format_timestamp(first)}")
    print(f"Last Request:       {format_timestamp(last)}")

    client.close()


def export_conversation(job_id, output_path):
    """Export conversation to JSON file."""
    client, db_name = get_mongodb_client()
    db = client[db_name]
    collection = db["llm_requests"]

    cursor = collection.find({"job_id": job_id}).sort("timestamp", 1)
    records = list(cursor)

    if not records:
        print(f"No records found for job: {job_id}")
        return

    # Convert ObjectId and datetime for JSON serialization
    def serialize(obj):
        if hasattr(obj, "isoformat"):
            return obj.isoformat()
        if hasattr(obj, "__str__"):
            return str(obj)
        return obj

    for record in records:
        record["_id"] = str(record["_id"])
        if "timestamp" in record:
            record["timestamp"] = serialize(record["timestamp"])

    with open(output_path, "w") as f:
        json.dump(records, f, indent=2, default=serialize)

    print(f"Exported {len(records)} records to {output_path}")
    client.close()


def list_jobs():
    """List all jobs with LLM records."""
    client, db_name = get_mongodb_client()
    db = client[db_name]
    collection = db["llm_requests"]

    pipeline = [
        {
            "$group": {
                "_id": "$job_id",
                "count": {"$sum": 1},
                "agent_type": {"$first": "$agent_type"},
                "first_request": {"$min": "$timestamp"},
                "last_request": {"$max": "$timestamp"},
            }
        },
        {"$sort": {"last_request": -1}},
        {"$limit": 50},
    ]

    results = list(collection.aggregate(pipeline))

    if not results:
        print("No jobs found")
        return

    print(f"\n{'=' * 70}")
    print(f"Jobs with LLM Records (most recent first)")
    print(f"{'=' * 70}\n")

    for job in results:
        job_id = job["_id"]
        count = job.get("count", 0)
        agent = job.get("agent_type", "?")
        last = format_timestamp(job.get("last_request"))

        print(f"{job_id} | {agent:10} | {count:4} requests | last: {last}")

    client.close()


def main():
    parser = argparse.ArgumentParser(
        description="View LLM conversation history from MongoDB",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    parser.add_argument(
        "--job-id", "-j",
        help="Job ID to view",
    )
    parser.add_argument(
        "--recent", "-r",
        type=int,
        default=0,
        help="View N most recent requests across all jobs",
    )
    parser.add_argument(
        "--stats", "-s",
        action="store_true",
        help="Show statistics for job",
    )
    parser.add_argument(
        "--export", "-e",
        help="Export conversation to JSON file",
    )
    parser.add_argument(
        "--tools-only", "-t",
        action="store_true",
        help="Show only tool calls",
    )
    parser.add_argument(
        "--list", "-l",
        action="store_true",
        help="List all jobs with records",
    )
    parser.add_argument(
        "--agent-type", "-a",
        help="Filter by agent type",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Maximum records to show (default: 100)",
    )

    args = parser.parse_args()

    try:
        if args.list:
            list_jobs()
        elif args.recent > 0:
            view_recent(args.recent, args.agent_type)
        elif args.job_id:
            if args.stats:
                get_job_stats(args.job_id)
            elif args.export:
                export_conversation(args.job_id, args.export)
            else:
                view_conversation(args.job_id, args.limit, args.tools_only)
        else:
            parser.print_help()

    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
