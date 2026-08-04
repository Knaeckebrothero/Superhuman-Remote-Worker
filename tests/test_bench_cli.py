"""Thin-client payload and formatting tests; no live orchestrator required."""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

BENCH_DIR = Path(__file__).parents[1] / "bench"


def _load_script(name: str, filename: str):
    sys.path.insert(0, str(BENCH_DIR))
    try:
        spec = importlib.util.spec_from_file_location(name, BENCH_DIR / filename)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(BENCH_DIR))


def test_submit_server_payload_inlines_resolved_tasks():
    submit = _load_script("bench_submit_test", "submit.py")
    tasks = submit.load_tasks(BENCH_DIR / "tasks.yaml", {"S1-outbox-note"})
    args = argparse.Namespace(
        arm="candidate",
        model="model-pin",
        config_name=None,
        expert_id="11111111-1111-1111-1111-111111111111",
        run_id="nightly",
        replicates=3,
        max_in_flight=2,
        project_id="22222222-2222-2222-2222-222222222222",
    )

    payload = submit.server_payload(tasks, args)

    assert payload["name"] == "nightly"
    assert payload["tasks"] == tasks
    assert payload["tasks"][0]["description"]
    assert payload["tasks"][0]["required_deliverables"] == ["output/outbox-note.md"]
    assert payload["arms"] == [
        {
            "name": "candidate",
            "model": "model-pin",
            "config_override": {},
            "expert_id": "11111111-1111-1111-1111-111111111111",
        }
    ]
    assert payload["project_id"] == "22222222-2222-2222-2222-222222222222"


def test_report_server_mode_only_formats_server_metrics(capsys):
    report = _load_script("bench_report_test", "report.py")
    response = {
        "run_id": "run-uuid",
        "name": "nightly",
        "status": "done",
        "jobs": [
            {
                "task": "S1",
                "arm": "baseline",
                "replicate": 1,
                "status": "failed",
                "classification": "infra",
                "request_count": 0,
                "wall_minutes": None,
                "median_prompt_tokens": None,
                "strategic_share_latency_pct": None,
                "strategic_share_prompt_tokens_pct": None,
            }
        ],
        "aggregates": [
            {
                "task": "S1",
                "arm": "baseline",
                "terminal_jobs": 1,
                "completed_jobs": 0,
                "infra_jobs": 1,
                "metrics": {
                    "wall_minutes": {
                        "median": 4,
                        "min": 3,
                        "max": 5,
                    }
                },
            }
        ],
    }

    report.print_server_report(response)

    output = capsys.readouterr().out
    assert "server Job Bench run run-uuid" in output
    assert "INFRA" in output
    assert "4 [3..5]" in output
