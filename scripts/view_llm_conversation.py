#!/usr/bin/env python3
"""View LLM conversation history and agent audit trail from MongoDB.

This script queries the MongoDB archive to display LLM request/response
history and complete agent execution audit trail for debugging and analysis.

Usage:
    # View all LLM requests for a job
    python scripts/view_llm_conversation.py --job-id <uuid>

    # View complete audit trail (all agent steps)
    python scripts/view_llm_conversation.py --job-id <uuid> --audit

    # View only tool calls in audit trail
    python scripts/view_llm_conversation.py --job-id <uuid> --audit --step-type tool_call

    # View audit timeline (node transitions)
    python scripts/view_llm_conversation.py --job-id <uuid> --audit --timeline

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


# =============================================================================
# Agent Audit Trail Functions
# =============================================================================

def view_audit_trail(job_id, step_type=None, limit=100):
    """View complete audit trail for a job."""
    client, db_name = get_mongodb_client()
    db = client[db_name]
    collection = db["agent_audit"]

    # Build query
    query = {"job_id": job_id}
    if step_type:
        query["step_type"] = step_type

    cursor = collection.find(query).sort("step_number", 1).limit(limit)
    records = list(cursor)

    if not records:
        print(f"No audit records found for job: {job_id}")
        if step_type:
            print(f"(filtered by step_type: {step_type})")
        return

    print(f"\n{'=' * 80}")
    print(f"Agent Audit Trail: {job_id}")
    print(f"Found {len(records)} audit records" + (f" (step_type: {step_type})" if step_type else ""))
    print(f"{'=' * 80}\n")

    # Color coding for step types
    step_colors = {
        "initialize": "\033[36m",    # Cyan
        "llm_call": "\033[33m",      # Yellow
        "llm_response": "\033[32m",  # Green
        "tool_call": "\033[35m",     # Magenta
        "tool_result": "\033[34m",   # Blue
        "check": "\033[37m",         # White
        "routing": "\033[90m",       # Gray
        "error": "\033[31m",         # Red
    }
    reset = "\033[0m"

    for record in records:
        step_num = record.get("step_number", "?")
        iteration = record.get("iteration", "?")
        step_type_val = record.get("step_type", "?")
        node_name = record.get("node_name", "?")
        ts = format_timestamp(record.get("timestamp"))
        latency = record.get("latency_ms")

        color = step_colors.get(step_type_val, "")

        # Header line
        latency_str = f" | {latency}ms" if latency else ""
        print(f"{color}[{step_num:3}] iter={iteration:3} | {step_type_val:12} | {node_name:10}{latency_str}{reset}")

        # Step-specific details
        if step_type_val == "initialize":
            state = record.get("state", {})
            if state.get("document_path"):
                print(f"      Document: {state['document_path']}")

        elif step_type_val == "llm_call":
            llm = record.get("llm", {})
            print(f"      Model: {llm.get('model', '?')}, Input messages: {llm.get('input_message_count', '?')}")
            state = record.get("state", {})
            print(f"      Context: {state.get('message_count', '?')} msgs, {state.get('token_count', '?')} tokens")

        elif step_type_val == "llm_response":
            llm = record.get("llm", {})
            metrics = llm.get("metrics", {})
            tool_calls = llm.get("tool_calls", [])
            print(f"      Output: {metrics.get('output_chars', '?')} chars, {metrics.get('tool_call_count', 0)} tools")
            if tool_calls:
                tools_str = ", ".join([tc.get("name", "?") for tc in tool_calls[:5]])
                print(f"      Tools: {tools_str}")
            preview = llm.get("response_content_preview", "")
            if preview:
                print(f"      Preview: {truncate(preview, 100)}")

        elif step_type_val == "tool_call":
            tool = record.get("tool", {})
            args = tool.get("arguments", {})
            args_str = ", ".join([f"{k}={truncate(str(v), 30)}" for k, v in list(args.items())[:3]])
            print(f"      Tool: {tool.get('name', '?')}({args_str})")

        elif step_type_val == "tool_result":
            tool = record.get("tool", {})
            success = tool.get("success", False)
            status = "\033[32mOK\033[0m" if success else "\033[31mFAIL\033[0m"
            print(f"      Tool: {tool.get('name', '?')} -> {status}")
            if tool.get("error"):
                print(f"      Error: {truncate(tool['error'], 80)}")
            elif tool.get("result_preview"):
                print(f"      Result: {truncate(tool['result_preview'], 80)}")

        elif step_type_val == "check":
            check = record.get("check", {})
            print(f"      Decision: {check.get('decision', '?')} ({check.get('reason', '?')})")
            state = record.get("state", {})
            print(f"      State: {state.get('message_count', '?')} msgs, {state.get('token_count', '?')} tokens")

        elif step_type_val == "error":
            error = record.get("error", {})
            print(f"      Type: {error.get('type', '?')}")
            print(f"      Message: {truncate(error.get('message', ''), 100)}")

        print()

    client.close()


def view_audit_timeline(job_id, limit=500):
    """View audit trail as a timeline of node transitions."""
    client, db_name = get_mongodb_client()
    db = client[db_name]
    collection = db["agent_audit"]

    cursor = collection.find({"job_id": job_id}).sort("step_number", 1).limit(limit)
    records = list(cursor)

    if not records:
        print(f"No audit records found for job: {job_id}")
        return

    print(f"\n{'=' * 80}")
    print(f"Agent Execution Timeline: {job_id}")
    print(f"{'=' * 80}\n")

    # Group by iteration
    iterations = {}
    for record in records:
        iteration = record.get("iteration", 0)
        if iteration not in iterations:
            iterations[iteration] = []
        iterations[iteration].append(record)

    # Node symbols
    symbols = {
        "initialize": "I",
        "process": "P",
        "tools": "T",
        "check": "C",
    }

    step_symbols = {
        "initialize": "\033[36mINIT\033[0m",
        "llm_call": "\033[33mLLM→\033[0m",
        "llm_response": "\033[32m→LLM\033[0m",
        "tool_call": "\033[35mTOOL→\033[0m",
        "tool_result": "\033[34m→TOOL\033[0m",
        "check": "\033[37mCHK\033[0m",
        "routing": "\033[90mROUTE\033[0m",
        "error": "\033[31mERR\033[0m",
    }

    for iteration in sorted(iterations.keys()):
        steps = iterations[iteration]
        step_strs = []
        for step in steps:
            step_type = step.get("step_type", "?")
            symbol = step_symbols.get(step_type, step_type[:4])
            step_strs.append(symbol)

        print(f"Iter {iteration:3}: " + " → ".join(step_strs))

    # Summary
    print(f"\n{'-' * 80}")
    print("Summary:")

    # Count step types
    step_counts = {}
    total_latency = 0
    for record in records:
        st = record.get("step_type", "unknown")
        step_counts[st] = step_counts.get(st, 0) + 1
        if record.get("latency_ms"):
            total_latency += record["latency_ms"]

    for st, count in sorted(step_counts.items()):
        print(f"  {st:15}: {count:4}")

    print(f"\nTotal steps: {len(records)}")
    print(f"Total iterations: {len(iterations)}")
    print(f"Total latency: {total_latency}ms ({total_latency/1000:.1f}s)")

    client.close()


def get_audit_stats(job_id):
    """Get audit statistics for a job."""
    client, db_name = get_mongodb_client()
    db = client[db_name]
    collection = db["agent_audit"]

    # Check if any records exist
    count = collection.count_documents({"job_id": job_id})
    if count == 0:
        print(f"No audit records found for job: {job_id}")
        return

    # Aggregate by step type
    pipeline = [
        {"$match": {"job_id": job_id}},
        {
            "$group": {
                "_id": "$step_type",
                "count": {"$sum": 1},
                "avg_latency_ms": {"$avg": "$latency_ms"},
                "total_latency_ms": {"$sum": "$latency_ms"},
            }
        },
        {"$sort": {"count": -1}},
    ]

    step_results = list(collection.aggregate(pipeline))

    # Get timing info
    time_pipeline = [
        {"$match": {"job_id": job_id}},
        {
            "$group": {
                "_id": None,
                "first_step": {"$min": "$timestamp"},
                "last_step": {"$max": "$timestamp"},
                "max_iteration": {"$max": "$iteration"},
                "total_steps": {"$sum": 1},
            }
        },
    ]
    time_results = list(collection.aggregate(time_pipeline))
    time_info = time_results[0] if time_results else {}

    # Get tool stats
    tool_pipeline = [
        {"$match": {"job_id": job_id, "step_type": "tool_result"}},
        {
            "$group": {
                "_id": "$tool.name",
                "calls": {"$sum": 1},
                "successes": {"$sum": {"$cond": ["$tool.success", 1, 0]}},
                "failures": {"$sum": {"$cond": ["$tool.success", 0, 1]}},
                "avg_latency_ms": {"$avg": "$latency_ms"},
            }
        },
        {"$sort": {"calls": -1}},
    ]
    tool_results = list(collection.aggregate(tool_pipeline))

    print(f"\n{'=' * 80}")
    print(f"Agent Audit Statistics: {job_id}")
    print(f"{'=' * 80}\n")

    # Overview
    first = time_info.get("first_step")
    last = time_info.get("last_step")
    duration = (last - first).total_seconds() if first and last else 0

    print("Overview:")
    print(f"  Total Steps:      {time_info.get('total_steps', 0)}")
    print(f"  Max Iteration:    {time_info.get('max_iteration', 0)}")
    print(f"  Duration:         {duration:.1f}s")
    print(f"  First Step:       {format_timestamp(first)}")
    print(f"  Last Step:        {format_timestamp(last)}")
    print()

    # Step type breakdown
    print("Step Type Breakdown:")
    total_latency = 0
    for r in step_results:
        step_type = r["_id"]
        count = r["count"]
        avg_lat = r.get("avg_latency_ms")
        total_lat = r.get("total_latency_ms", 0)
        total_latency += total_lat or 0

        lat_str = f"avg={avg_lat:.0f}ms" if avg_lat else ""
        print(f"  {step_type:15}: {count:4} {lat_str}")

    print(f"\n  Total Latency: {total_latency:.0f}ms ({total_latency/1000:.1f}s)")
    print()

    # Tool breakdown
    if tool_results:
        print("Tool Breakdown:")
        for r in tool_results:
            tool_name = r["_id"] or "unknown"
            calls = r["calls"]
            successes = r["successes"]
            failures = r["failures"]
            avg_lat = r.get("avg_latency_ms")

            success_rate = (successes / calls * 100) if calls > 0 else 0
            lat_str = f"avg={avg_lat:.0f}ms" if avg_lat else ""
            print(f"  {tool_name:20}: {calls:3} calls ({success_rate:.0f}% success) {lat_str}")

    client.close()


def main():
    parser = argparse.ArgumentParser(
        description="View LLM conversation history and agent audit trail from MongoDB",
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

    # Agent audit trail options
    parser.add_argument(
        "--audit",
        action="store_true",
        help="View agent audit trail instead of LLM requests",
    )
    parser.add_argument(
        "--step-type",
        choices=["initialize", "llm_call", "llm_response", "tool_call", "tool_result", "check", "routing", "error"],
        help="Filter audit trail by step type",
    )
    parser.add_argument(
        "--timeline",
        action="store_true",
        help="Show audit trail as timeline visualization",
    )

    args = parser.parse_args()

    try:
        if args.list:
            list_jobs()
        elif args.recent > 0:
            view_recent(args.recent, args.agent_type)
        elif args.job_id:
            if args.audit:
                # Audit trail viewing
                if args.timeline:
                    view_audit_timeline(args.job_id, args.limit)
                elif args.stats:
                    get_audit_stats(args.job_id)
                else:
                    view_audit_trail(args.job_id, args.step_type, args.limit)
            elif args.stats:
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
