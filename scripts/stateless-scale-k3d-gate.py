#!/usr/bin/env python3
"""Repeat the six-turn resilience timing gate against the local Tilt release.

Uses disposable threads and the same twelve-line poem prompt as the original
scale test. Credentials remain in memory. Prints measurements before asserting
the original targets, so a partial result cannot accidentally look like a pass.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import re
import time


def main():
    path = Path(__file__).with_name("stateless-resilience-k3d-gate.py")
    spec = importlib.util.spec_from_file_location("gate", path)
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    assert (
        gate.sql("SELECT count(*) FROM run_queue WHERE state IN ('queued','leased')")
        == "0"
    )

    def deployment():
        return json.loads(
            gate.command(
                gate.K + ["get", "deploy", "srw-agent-stateless", "-o", "json"]
            )
        )

    gate.wait_for(
        "pool at floor two", lambda: deployment()["status"].get("replicas") == 2
    )

    def current_runtime():
        dep = deployment()
        return (
            dep["status"].get("updatedReplicas") == dep["spec"]["replicas"]
            and dep["status"].get("readyReplicas") == dep["spec"]["replicas"]
            and dep["status"].get("replicas") == dep["spec"]["replicas"]
            and gate.current_runtime()
        )

    gate.wait_for("deployed runtime matches the current source", current_runtime)
    client = gate.Gate()
    threads = []
    for index in range(6):
        response = client.api(
            "/api/persistent/threads",
            {
                "title": f"resilience scale {index + 1}",
                "config_name": "session_base",
            },
        )
        threads.append(response.get("id") or response["thread_id"])
    print("threads=" + json.dumps(threads), flush=True)
    ids = ",".join("'" + thread + "'" for thread in threads)
    started = time.monotonic()
    started_wall = datetime.now(timezone.utc)
    since = started_wall.isoformat().replace("+00:00", "Z")

    def send(index):
        client.api(
            f"/api/persistent/threads/{threads[index]}/input",
            {
                "content": f"Scale test {index + 1}: write a 12-line poem about waiting rooms, then end with the exact token SCALE-OK-{index + 1}.",
            },
        )

    with ThreadPoolExecutor(max_workers=6) as executor:
        list(executor.map(send, range(6)))
    six_at = None
    released_at = None
    last_reply_age_at_release = None
    floor_at = None
    timings = {}
    rows = []
    end = started + 1000
    next_report = 0
    while time.monotonic() < end:
        elapsed = time.monotonic() - started
        dep = deployment()
        if dep["spec"]["replicas"] >= 6 and six_at is None:
            hpa = json.loads(
                gate.command(
                    gate.K
                    + ["get", "hpa", "keda-hpa-srw-agent-stateless", "-o", "json"]
                )
            )
            # Use the controller's timestamp, avoiding two-second sample jitter.
            scaled_at = datetime.fromisoformat(
                hpa["status"]["lastScaleTime"].replace("Z", "+00:00")
            )
            six_at = (scaled_at - started_wall).total_seconds()
        rows = json.loads(
            gate.sql(f"""
            SELECT coalesce(json_agg(row_to_json(s)), '[]') FROM (
              SELECT q.unit_id, q.state,
                (SELECT count(*) FROM thread_messages WHERE thread_id=q.unit_id AND role='human') AS humans,
                (SELECT count(*) FROM thread_messages WHERE thread_id=q.unit_id AND role='ai') AS answers,
                (SELECT min(content) FROM thread_messages WHERE thread_id=q.unit_id AND role='ai') AS content,
                (SELECT extract(epoch FROM (max(created_at) FILTER (WHERE role='ai') - min(created_at) FILTER (WHERE role='human'))) FROM thread_messages WHERE thread_id=q.unit_id) AS reply_seconds,
                (SELECT extract(epoch FROM (now() - max(created_at))) FROM thread_messages WHERE thread_id=q.unit_id AND role='ai') AS reply_age
              FROM run_queue q WHERE q.unit_id IN ({ids})
            ) s
        """)
        )
        assert not any(row["state"] == "parked" for row in rows), "test turn parked"
        if len(rows) == 6 and all(
            row["state"] == "done" and row["answers"] == 1 for row in rows
        ):
            if released_at is None:
                released_at = elapsed
                last_reply_age_at_release = float(min(row["reply_age"] for row in rows))
            if dep["status"].get("replicas") == 2 and dep["spec"]["replicas"] == 2:
                floor_at = elapsed
        pods = json.loads(
            gate.command(
                gate.K
                + ["get", "pods", "-l", "srw/class=agent-stateless", "-o", "json"]
            )
        )["items"]
        for pod in pods:
            if pod["status"].get("phase") != "Running":
                continue
            try:
                logs = gate.command(
                    gate.K
                    + [
                        "logs",
                        pod["metadata"]["name"],
                        "-c",
                        "agent",
                        "--since-time=" + since,
                    ]
                )
            except RuntimeError:
                continue  # A pod can disappear between list and logs.
            for line in logs.splitlines():
                if "turn timing:" not in line:
                    continue
                match = re.search(r"unit=([0-9a-f-]+)", line)
                if match and match[1] in threads:
                    timings[match[1]] = {
                        key: float(value)
                        for key, value in re.findall(r"(\w+)=([0-9.]+)s", line)
                    }
        if elapsed >= next_report:
            print(
                json.dumps(
                    {
                        "elapsed": round(elapsed, 1),
                        "desired": dep["spec"]["replicas"],
                        "replies": sum(row["answers"] for row in rows),
                        "active": sum(row["state"] != "done" for row in rows),
                    }
                ),
                flush=True,
            )
            next_report = elapsed + 30
        if floor_at is not None and len(timings) == 6:
            break
        time.sleep(2)

    report = {
        "threads": threads,
        "hpa_two_to_six_seconds": six_at,
        "lease_release_after_last_reply_seconds": last_reply_age_at_release,
        "floor_after_release_seconds": None
        if floor_at is None or released_at is None
        else floor_at - released_at,
        "turns": [
            {
                "thread": thread,
                "reply_seconds": next(
                    (r["reply_seconds"] for r in rows if r["unit_id"] == thread), None
                ),
                "expected_token_present": f"SCALE-OK-{index + 1}"
                in next(
                    (r["content"] or "" for r in rows if r["unit_id"] == thread), ""
                ),
                **timings.get(thread, {}),
            }
            for index, thread in enumerate(threads)
        ],
    }
    print(json.dumps(report, indent=2), flush=True)
    failures = []
    if six_at is None or six_at > 15:
        failures.append("HPA 2→6 exceeded 15 seconds")
    if last_reply_age_at_release is None or last_reply_age_at_release > 30:
        failures.append("leases did not clear within 30 seconds of last reply")
    if floor_at is None:
        failures.append("pool did not return to floor")
    if len(timings) != 6 or any(t["total"] > 26 for t in timings.values()):
        failures.append(
            "one or more slot holds exceeded the original 26-second estimate"
        )
    for index, thread in enumerate(threads):
        row = next((r for r in rows if r["unit_id"] == thread), {})
        if (
            row.get("humans") != 1
            or row.get("answers") != 1
            or len(re.sub(r"</?think>", "", row.get("content") or "").split()) < 12
        ):
            failures.append(
                f"turn {index + 1} did not produce exactly one valid answer"
            )
        if row.get("reply_seconds") is None or row["reply_seconds"] > 37:
            failures.append(f"turn {index + 1} exceeded 37-second first-reply target")
    assert not failures, "; ".join(failures)
    print("PASS six-turn scale gate", flush=True)


if __name__ == "__main__":
    main()
