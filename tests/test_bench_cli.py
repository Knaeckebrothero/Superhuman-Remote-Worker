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


def test_submit_server_payload_builds_one_arm_per_bundled_config():
    submit = _load_script("bench_submit_arms_test", "submit.py")
    tasks = submit.load_tasks(BENCH_DIR / "tasks.yaml", {"D1-wordfreq-kata"})
    args = argparse.Namespace(
        arm="baseline",
        arms="developer,engineer",
        model="MiniMax-M3",
        config_name=None,
        expert_id=None,
        run_id="dev-vs-eng-01",
        replicates=3,
        max_in_flight=2,
        project_id=None,
    )
    payload = submit.server_payload(tasks, args)
    assert payload["arms"] == [
        {
            "name": "developer",
            "config_name": "developer",
            "config_override": {},
            "model": "MiniMax-M3",
        },
        {
            "name": "engineer",
            "config_name": "engineer",
            "config_override": {},
            "model": "MiniMax-M3",
        },
    ]
    assert "project_id" not in payload


def test_new_bench_tasks_are_pinned_to_the_engineer_and_self_describing():
    submit = _load_script("bench_tasks_test", "submit.py")
    ids = {
        "D3-ledger-refactor",
        "D4-static-page",
        "D5-clone-and-extend",
        "O1-install-and-report",
    }
    tasks = {t["id"]: t for t in submit.load_tasks(BENCH_DIR / "tasks.yaml", ids)}
    assert set(tasks) == ids
    for task in tasks.values():
        assert task["config_name"] == "engineer", task["id"]
        assert task["required_deliverables"], task["id"]
        assert "output/" in " ".join(task["required_deliverables"]), task["id"]
    assert tasks["D5-clone-and-extend"]["family"] == "dev-repo"
    assert tasks["O1-install-and-report"]["family"] == "ops"


def test_submit_server_payload_assigns_per_arm_projects_when_given():
    submit = _load_script("bench_submit_isolate_test", "submit.py")
    tasks = submit.load_tasks(BENCH_DIR / "tasks.yaml", {"D3-ledger-refactor"})
    args = argparse.Namespace(
        arm="baseline",
        arms="developer,engineer",
        model="MiniMax-M3",
        config_name=None,
        expert_id=None,
        run_id="dev-vs-eng-01",
        replicates=3,
        max_in_flight=2,
        project_id=None,
    )
    projects = {
        "developer": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "engineer": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    }
    payload = submit.server_payload(tasks, args, arm_projects=projects)
    assert [a["project_id"] for a in payload["arms"]] == [
        projects["developer"],
        projects["engineer"],
    ]
    # Without the mapping no arm carries a project (they inherit the run's).
    plain = submit.server_payload(tasks, args)
    assert all("project_id" not in a for a in plain["arms"])


def test_create_arm_projects_makes_one_throwaway_project_per_arm(monkeypatch):
    submit = _load_script("bench_submit_projects_test", "submit.py")
    calls: list[tuple[str, dict]] = []

    def fake_post(path, payload):
        calls.append((path, payload))
        return {"id": f"pid-{payload['name']}"}

    monkeypatch.setattr(submit, "post", fake_post)
    projects = submit.create_arm_projects(
        "dev-vs-eng-01", ["developer", "engineer"], "uid-1"
    )
    assert projects == {
        "developer": "pid-bench-dev-vs-eng-01-developer",
        "engineer": "pid-bench-dev-vs-eng-01-engineer",
    }
    assert [c[0] for c in calls] == ["/api/projects", "/api/projects"]
    for _path, payload in calls:
        assert payload["user_id"] == "uid-1"
        assert "dev-vs-eng-01" in payload["goal"]
