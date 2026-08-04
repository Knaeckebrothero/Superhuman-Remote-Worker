#!/usr/bin/env python3
"""Report on a local or orchestrator-native Job Bench run.

The default v0 mode reads a local manifest and computes the report client-side.
``--server`` fetches the already-computed v1 report from the orchestrator and
only formats it; request/archive data never crosses into client-side analysis.
"""

from __future__ import annotations

import argparse
import json
import statistics as st
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _api import ApiError, get  # noqa: E402

TERMINAL = {"completed", "failed", "cancelled", "pending_review", "waiting_for_reply"}
BENCH_DIR = Path(__file__).parent
TAIL_ANOMALY_FRACTION = 0.15


def parse_ts(value: str) -> datetime:
    """Parse an API timestamp; naive values (archive filenames) are UTC."""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00").replace(" ", "T"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def fetch_requests(job_id: str) -> list[dict]:
    """All main-loop llm requests, oldest first, normalized fields."""
    rows, offset = [], 0
    while True:
        data = get(
            f"/api/jobs/{job_id}/llm-requests",
            limit=100,
            offset=offset,
            call_type="main",
        )
        items = data.get("requests") or data.get("items") or data.get("entries") or []
        for item in items:
            usage = (
                item.get("token_usage")
                or item.get("usage")
                or (item.get("metrics") or {}).get("token_usage")
                or {}
            )
            in_tok = (
                usage.get("prompt_tokens")
                or usage.get("input_tokens")
                or item.get("input_tokens")
                or 0
            )
            out_tok = (
                usage.get("completion_tokens")
                or usage.get("output_tokens")
                or item.get("output_tokens")
                or 0
            )
            ts_raw = item.get("timestamp") or item.get("created_at")
            if not ts_raw:
                continue
            rows.append(
                {
                    "ts": parse_ts(ts_raw),
                    "in": int(in_tok or 0),
                    "out": int(out_tok or 0),
                    "lat": int(item.get("latency_ms") or 0),
                }
            )
        if len(items) < 100:
            break
        offset += 100
    rows.sort(key=lambda r: r["ts"])
    return rows


def fetch_archives(
    job_id: str, not_before: datetime | None
) -> list[tuple[str, datetime]]:
    """Phase archive events as (S|T, ts), oldest first.

    not_before filters out archives inherited from a parent job's workspace
    (critic subjobs list the parent's files too).
    """
    entries = get(f"/api/jobs/{job_id}/todos/archives") or []
    events = []
    for e in entries:
        name = e.get("phase_name") or e.get("filename") or ""
        ts_raw = e.get("timestamp")
        if not ts_raw:
            continue
        typ = "S" if "strategic" in name else ("T" if "tactical" in name else None)
        if typ is None:
            continue
        ts = parse_ts(ts_raw)
        if not_before and ts < not_before:
            continue
        events.append((typ, ts))
    events.sort(key=lambda x: x[1])
    return events


def analyze_job(job_id: str) -> dict:
    job = get(f"/api/jobs/{job_id}") or {}
    status = job.get("status") or job.get("job", {}).get("status")
    created = job.get("created_at") or job.get("created")
    reqs = fetch_requests(job_id)
    result = {"job_id": job_id, "status": status, "n_req": len(reqs)}
    if not reqs:
        return result

    t0, t1 = reqs[0]["ts"], reqs[-1]["ts"]
    events = fetch_archives(job_id, parse_ts(created) if created else None)
    result.update(
        {
            "wall_min": round((t1 - t0).total_seconds() / 60, 1),
            "in_ktok": round(sum(r["in"] for r in reqs) / 1000),
            "out_ktok": round(sum(r["out"] for r in reqs) / 1000),
            "med_in": int(st.median(r["in"] for r in reqs)),
            "arch_S": sum(1 for t, _ in events if t == "S"),
            "arch_T": sum(1 for t, _ in events if t == "T"),
        }
    )
    if not events:
        return result

    # Segment attribution: each archive event closes a segment of that type;
    # requests after the last archive belong to the final strategic wrap.
    acc = {"S": [0, 0, 0], "T": [0, 0, 0]}  # llm_ms, n, in_tok
    prev = t0
    tail_n = sum(1 for r in reqs if r["ts"] > events[-1][1])
    for typ, et in events + [("S", t1)]:
        seg = [
            r for r in reqs if prev < r["ts"] <= et or (prev == t0 and r["ts"] <= et)
        ]
        prev_first = prev == t0
        if prev_first:
            seg = [r for r in reqs if r["ts"] <= et]
        acc[typ][0] += sum(r["lat"] for r in seg)
        acc[typ][1] += len(seg)
        acc[typ][2] += sum(r["in"] for r in seg)
        prev = et

    def share(idx):
        s, t = acc["S"][idx], acc["T"][idx]
        return round(100 * s / (s + t), 1) if (s + t) else None

    result.update(
        {
            # Job Bench v1 surfaces latency_ms on the audit summary. Keep the
            # token fallback so local mode still works against an older server.
            "strat_share_llm": share(0),
            "strat_share_req": share(1),
            "strat_share_intok": share(2),
            "strat_share": share(0) if acc["S"][0] + acc["T"][0] else share(2),
            "tail_anomaly": tail_n / len(reqs) > TAIL_ANOMALY_FRACTION,
            "tail_req": tail_n,
        }
    )
    return result


def med_range(values):
    values = [v for v in values if v is not None]
    if not values:
        return "-"
    if len(values) == 1:
        return f"{values[0]:g}"
    return f"{st.median(values):g} [{min(values):g}..{max(values):g}]"


def summary_range(summary: dict | None) -> str:
    """Format a server ``{median,min,max}`` metric like the v0 table."""

    if not summary:
        return "-"
    median, minimum, maximum = (
        summary.get("median"),
        summary.get("min"),
        summary.get("max"),
    )
    if median is None:
        return "-"
    if minimum == maximum:
        return f"{median:g}"
    return f"{median:g} [{minimum:g}..{maximum:g}]"


def print_server_report(report: dict) -> None:
    """Render the server response without recomputing any metric."""

    print(
        f"== server Job Bench run {report.get('run_id')} "
        f"({report.get('name')!r}, {report.get('status')}) =="
    )
    for row in report.get("jobs") or []:
        classification = row.get("classification")
        infra = " INFRA" if classification == "infra" else ""
        tail = " TAIL-ANOMALY" if row.get("tail_anomaly") else ""
        print(
            f"  {row.get('task')} {row.get('arm')} r{row.get('replicate')} "
            f"{row.get('status')}: req={row.get('request_count')} "
            f"wall={row.get('wall_minutes')}m "
            f"med_in={row.get('median_prompt_tokens')} "
            f"strat_llm={row.get('strategic_share_latency_pct')}% "
            f"strat_tok={row.get('strategic_share_prompt_tokens_pct')}%"
            f"{infra}{tail}"
        )

    print("\n== per-task x arm across clean terminal replicates ==")
    header = (
        f"{'task':18} {'arm':14} {'n':>2} {'ok':>2} {'infra':>5} "
        f"{'wall_min':>16} {'n_req':>14} {'med_in':>20} "
        f"{'strat_llm%':>16} {'strat_tok%':>16}"
    )
    print(header)
    for row in report.get("aggregates") or []:
        metrics = row.get("metrics") or {}
        print(
            f"{str(row.get('task')):18} {str(row.get('arm')):14} "
            f"{row.get('terminal_jobs', 0):>2} {row.get('completed_jobs', 0):>2} "
            f"{row.get('infra_jobs', 0):>5} "
            f"{summary_range(metrics.get('wall_minutes')):>16} "
            f"{summary_range(metrics.get('request_count')):>14} "
            f"{summary_range(metrics.get('median_prompt_tokens')):>20} "
            f"{summary_range(metrics.get('strategic_share_latency_pct')):>16} "
            f"{summary_range(metrics.get('strategic_share_prompt_tokens_pct')):>16}"
        )


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run-id")
    ap.add_argument("--manifest")
    ap.add_argument(
        "--server",
        action="store_true",
        help="fetch /api/bench/runs/{id}/report instead of computing locally",
    )
    args = ap.parse_args()
    if args.server:
        if not args.run_id or args.manifest:
            raise SystemExit(
                "server mode requires --run-id and does not use --manifest"
            )
        print_server_report(get(f"/api/bench/runs/{args.run_id}/report"))
        return
    if not args.manifest and not args.run_id:
        raise SystemExit("pass --run-id or --manifest")
    manifest_path = (
        Path(args.manifest)
        if args.manifest
        else BENCH_DIR / "runs" / args.run_id / "manifest.json"
    )
    manifest = json.loads(manifest_path.read_text())

    per_job = []
    for entry in manifest["jobs"]:
        if not entry.get("job_id"):
            continue
        try:
            row = analyze_job(entry["job_id"])
        except ApiError as e:
            row = {"job_id": entry["job_id"], "status": f"api-error:{e.status}"}
        row.update(
            {
                "task": entry["task"],
                "replicate": entry["replicate"],
                "family": entry.get("family"),
            }
        )
        per_job.append(row)
        print(
            f"  {entry['task']} r{entry['replicate']} {row.get('status')}: "
            f"req={row.get('n_req')} wall={row.get('wall_min')}m "
            f"med_in={row.get('med_in')} strat={row.get('strat_share')}%"
            + (" TAIL-ANOMALY" if row.get("tail_anomaly") else "")
        )

    print(
        f"\n== per-task across replicates (terminal jobs only) — run "
        f"{manifest['run_id']}, pins {manifest.get('pins')} =="
    )
    hdr = (
        f"{'task':18} {'n':>2} {'ok':>2} {'wall_min':>16} {'n_req':>14} "
        f"{'med_in':>20} {'strat%':>16}"
    )
    print(hdr)
    tasks = sorted({r["task"] for r in per_job})
    for task in tasks:
        rows = [r for r in per_job if r["task"] == task and r.get("status") in TERMINAL]
        if not rows:
            continue
        ok = sum(1 for r in rows if r["status"] == "completed")
        clean = [r for r in rows if not r.get("tail_anomaly")]
        print(
            f"{task:18} {len(rows):>2} {ok:>2} "
            f"{med_range([r.get('wall_min') for r in clean]):>16} "
            f"{med_range([r.get('n_req') for r in clean]):>14} "
            f"{med_range([r.get('med_in') for r in clean]):>20} "
            f"{med_range([r.get('strat_share') for r in clean]):>16}"
        )

    out = manifest_path.parent / "report.json"
    out.write_text(
        json.dumps(
            {
                "run_id": manifest["run_id"],
                "pins": manifest.get("pins"),
                "jobs": per_job,
            },
            indent=1,
            default=str,
        )
    )
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
