#!/usr/bin/env python3
"""Submit a local or orchestrator-native Job Bench run.

The default v0 mode writes ``bench/runs/<run-id>/manifest.json`` and keeps the
throttling process on this machine. ``--server`` instead reads and resolves the
same local task YAML, posts the complete frozen spec to the orchestrator, and
returns immediately; its sweeper owns submission and restart recovery.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))
from _api import ApiError, get, post  # noqa: E402

TERMINAL = {"completed", "failed", "cancelled", "pending_review", "waiting_for_reply"}
BENCH_DIR = Path(__file__).parent


def load_tasks(path: Path, only: set[str] | None):
    spec = yaml.safe_load(path.read_text())
    defaults = spec.get("defaults", {})
    tasks = []
    for t in spec["tasks"]:
        if only and t["id"] not in only:
            continue
        tasks.append({**defaults, **t})
    if only:
        missing = only - {t["id"] for t in tasks}
        if missing:
            raise SystemExit(f"unknown task ids: {sorted(missing)}")
    return tasks


def load_manifest(path: Path):
    if path.exists():
        return json.loads(path.read_text())
    return {"run_id": None, "created": None, "pins": {}, "jobs": []}


def save_manifest(path: Path, manifest: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(manifest, indent=1))
    tmp.replace(path)


def job_status(job_id: str) -> str | None:
    """Current status, or None on transient network/API failure.

    A DNS blip or gateway hiccup during an overnight run must degrade to
    "unknown, check again next poll" — an uncaught URLError here killed
    the first baseline run 8 jobs in.
    """
    try:
        data = get(f"/api/jobs/{job_id}")
    except (ApiError, OSError) as e:
        print(f"poll failed for {job_id} (transient, will retry): {e}", flush=True)
        return None
    return data.get("status") or data.get("job", {}).get("status") or "?"


def in_flight(manifest: dict) -> list[dict]:
    live = []
    for entry in manifest["jobs"]:
        if entry.get("job_id") and entry.get("final_status") is None:
            status = job_status(entry["job_id"])
            if status is None:
                live.append(entry)  # unknown -> assume still running
                continue
            entry["last_status"] = status
            if status in TERMINAL:
                entry["final_status"] = status
                entry["finished_at"] = datetime.now(timezone.utc).isoformat()
            else:
                live.append(entry)
    return live


def submit_one(task: dict, args, replicate: int) -> str:
    override = {"llm": {"model": args.model}, "autonomy": "full"}
    for key, val in (task.get("config_override") or {}).items():
        override[key] = val
    payload = {
        "description": task["description"],
        "config_name": task.get("config_name", "worker_base"),
        "config_override": override,
        "priority": task.get("priority", 5),
        "context": {
            "bench": {
                "run_id": args.run_id,
                "task": task["id"],
                "family": task.get("family"),
                "arm": args.arm,
                "replicate": replicate,
                "model": args.model,
            }
        },
    }
    if task.get("required_deliverables"):
        payload["required_deliverables"] = task["required_deliverables"]
    if args.project_id:
        payload["project_id"] = args.project_id
    data = post("/api/jobs", payload)
    job_id = data.get("job_id") or data.get("id") or data.get("job", {}).get("id")
    if not job_id:
        raise RuntimeError(f"no job id in create response: {data}")
    return job_id


def server_payload(tasks: list[dict], args: argparse.Namespace) -> dict:
    """Build the inline v1 server spec without retaining a tasks-file link."""

    arm: dict = {
        "name": args.arm,
        "config_override": {},
        "model": args.model,
    }
    if args.config_name:
        arm["config_name"] = args.config_name
    if args.expert_id:
        arm["expert_id"] = args.expert_id
    payload = {
        "name": args.run_id,
        "tasks": tasks,
        "replicates": args.replicates,
        "max_in_flight": args.max_in_flight,
        "arms": [arm],
    }
    if args.project_id:
        payload["project_id"] = args.project_id
    return payload


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--tasks", default=str(BENCH_DIR / "tasks.yaml"))
    ap.add_argument("--only", help="comma-separated task ids")
    ap.add_argument("--replicates", type=int, default=3)
    ap.add_argument("--arm", default="baseline")
    ap.add_argument("--model", default="gemma-4-moe")
    ap.add_argument(
        "--config-name",
        help="server mode: arm-level bundled config (otherwise each task selects)",
    )
    ap.add_argument(
        "--expert-id",
        help="server mode: arm-level DB-backed expert UUID",
    )
    ap.add_argument("--project-id", default=None)
    ap.add_argument("--max-in-flight", type=int, default=2)
    ap.add_argument("--poll-seconds", type=int, default=60)
    ap.add_argument(
        "--no-wait",
        action="store_true",
        help="submit everything immediately and exit (no throttle)",
    )
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument(
        "--server",
        action="store_true",
        help="post the frozen spec to /api/bench/runs and exit",
    )
    args = ap.parse_args()

    only = set(args.only.split(",")) if args.only else None
    tasks = load_tasks(Path(args.tasks), only)
    if args.server:
        if args.resume:
            raise SystemExit(
                "--resume is local-mode only; server runs resume themselves"
            )
        if args.config_name and args.expert_id:
            raise SystemExit("pass --config-name or --expert-id, not both")
        payload = server_payload(tasks, args)
        if args.dry_run:
            print(json.dumps(payload, indent=2))
            return
        run = post("/api/bench/runs", payload)
        print(
            f"created server bench run {run['id']} "
            f"(name={run.get('name')!r}, status={run.get('status')})"
        )
        return

    manifest_path = BENCH_DIR / "runs" / args.run_id / "manifest.json"
    manifest = load_manifest(manifest_path)
    if manifest["jobs"] and not args.resume:
        raise SystemExit(f"{manifest_path} exists — pass --resume to continue it")
    manifest["run_id"] = args.run_id
    manifest.setdefault("created", datetime.now(timezone.utc).isoformat())
    manifest["pins"] = {
        "model": args.model,
        "arm": args.arm,
        "autonomy": "full",
        "tasks_file": args.tasks,
    }

    done = {(j["task"], j["replicate"]) for j in manifest["jobs"] if j.get("job_id")}
    queue = []
    for replicate in range(1, args.replicates + 1):
        wave = list(tasks)
        random.Random(f"{args.run_id}:{replicate}").shuffle(wave)
        for task in wave:
            if (task["id"], replicate) not in done:
                queue.append((task, replicate))

    print(
        f"run {args.run_id}: {len(queue)} jobs to submit "
        f"({len(done)} already in manifest)"
    )
    if args.dry_run:
        for task, replicate in queue:
            print(f"  would submit {task['id']} r{replicate}")
        return

    while queue:
        if not args.no_wait:
            while len(in_flight(manifest)) >= args.max_in_flight:
                save_manifest(manifest_path, manifest)
                time.sleep(args.poll_seconds)
        task, replicate = queue.pop(0)
        try:
            job_id = submit_one(task, args, replicate)
        except OSError as e:
            # Transient network failure: put the pair back and wait it out.
            print(
                f"submit blocked by network ({e}); retrying {task['id']} "
                f"r{replicate} in {args.poll_seconds}s",
                flush=True,
            )
            queue.insert(0, (task, replicate))
            time.sleep(args.poll_seconds)
            continue
        except ApiError as e:
            print(f"SUBMIT FAILED {task['id']} r{replicate}: {e}", flush=True)
            manifest["jobs"].append(
                {
                    "task": task["id"],
                    "replicate": replicate,
                    "job_id": None,
                    "error": str(e),
                    "submitted_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            save_manifest(manifest_path, manifest)
            continue
        print(f"submitted {task['id']} r{replicate} -> {job_id}", flush=True)
        manifest["jobs"].append(
            {
                "task": task["id"],
                "family": task.get("family"),
                "replicate": replicate,
                "job_id": job_id,
                "submitted_at": datetime.now(timezone.utc).isoformat(),
                "final_status": None,
            }
        )
        save_manifest(manifest_path, manifest)

    if not args.no_wait:
        while in_flight(manifest):
            save_manifest(manifest_path, manifest)
            time.sleep(args.poll_seconds)
    save_manifest(manifest_path, manifest)
    statuses = [j.get("final_status") or j.get("last_status") for j in manifest["jobs"]]
    print(
        f"done: {len(manifest['jobs'])} jobs, statuses: "
        f"{ {s: statuses.count(s) for s in set(statuses)} }"
    )
    print(f"manifest: {manifest_path}")


if __name__ == "__main__":
    main()
