"""Job Bench spec, queue, ownership, and restart-safe sweeper tests."""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from services.bench import (
    build_bench_job_payload,
    build_submission_queue,
    freeze_spec,
    sweep_run,
)

RUN_ID = "11111111-1111-1111-1111-111111111111"
USER_ID = "22222222-2222-2222-2222-222222222222"


def _spec(*, max_in_flight: int = 2, replicates: int = 1) -> dict:
    return freeze_spec(
        {
            "tasks": [
                {
                    "id": "t1",
                    "family": "small",
                    "description": "first task",
                    "required_deliverables": ["./output/a.md"],
                    "config_name": "defaults",
                    "config_override": {
                        "llm": {"temperature": 0.2, "model": "task-model"},
                        "nested": {"task": True, "winner": "task"},
                    },
                },
                {
                    "id": "t2",
                    "description": "second task",
                    "required_deliverables": ["output/b.md"],
                    "config_name": "developer",
                },
                {
                    "id": "t3",
                    "description": "third task",
                    "required_deliverables": [],
                },
            ],
            "replicates": replicates,
            "max_in_flight": max_in_flight,
            "arms": [
                {
                    "name": "baseline",
                    "model": "pinned-model",
                    "config_override": {
                        "nested": {"arm": True, "winner": "arm"},
                    },
                }
            ],
        }
    )


def _run(*, state: list[dict] | None = None, **spec_overrides) -> dict:
    return {
        "id": RUN_ID,
        "name": "test",
        "status": "running",
        "created_by": USER_ID,
        "spec": _spec(**spec_overrides),
        "state": copy.deepcopy(state or []),
    }


class FakeStore:
    """In-memory implementation of the BenchStore methods a sweep touches."""

    def __init__(self, run: dict, tagged_jobs: list[dict] | None = None):
        self.run = copy.deepcopy(run)
        self.tagged_jobs = copy.deepcopy(tagged_jobs or [])
        self.saves: list[dict] = []

    async def list_tagged_jobs(self, _run_id: str) -> list[dict]:
        return copy.deepcopy(self.tagged_jobs)

    async def save_run(
        self,
        _run_id: str,
        state: list[dict],
        *,
        status: str | None = None,
        require_status: str | None = None,
    ) -> dict | None:
        if require_status and self.run["status"] != require_status:
            return None
        self.run["state"] = copy.deepcopy(state)
        if status:
            self.run["status"] = status
        self.saves.append(copy.deepcopy(self.run))
        return copy.deepcopy(self.run)


def _tagged(entry: dict, status: str) -> dict:
    return {
        "id": entry["job_id"],
        "status": status,
        "created_at": datetime(2026, 8, 4, tzinfo=timezone.utc),
        "context": {
            "bench": {
                "run_id": RUN_ID,
                "task": entry["task"],
                "arm": entry["arm"],
                "replicate": entry["replicate"],
            }
        },
    }


def test_freeze_spec_is_complete_normalized_and_detached():
    source = {
        "tasks": [
            {
                "id": "task",
                "description": "Pinned description",
                "required_deliverables": ["./output/a.md", "output/a.md"],
                "config_name": "defaults",
                "config_override": {"extra": {"enabled": True}},
            }
        ],
        "replicates": 3,
        "max_in_flight": 2,
        "arms": [
            {
                "name": "candidate",
                "model": "model-pin",
                "config_override": {"guardrails": {"max": 4}},
            }
        ],
        "project_id": "33333333-3333-3333-3333-333333333333",
    }

    frozen = freeze_spec(source)
    source["tasks"][0]["description"] = "mutated later"
    source["tasks"][0]["config_override"]["extra"]["enabled"] = False
    source["arms"][0]["config_override"]["guardrails"]["max"] = 99

    assert frozen == {
        "version": 1,
        "tasks": [
            {
                "id": "task",
                "description": "Pinned description",
                "required_deliverables": ["output/a.md"],
                "config_name": "worker_base",
                "config_override": {"extra": {"enabled": True}},
                "priority": 5,
            }
        ],
        "replicates": 3,
        "max_in_flight": 2,
        "arms": [
            {
                "name": "candidate",
                "config_override": {"guardrails": {"max": 4}},
                "model": "model-pin",
            }
        ],
        "project_id": "33333333-3333-3333-3333-333333333333",
    }


def test_job_payload_is_owned_tagged_and_deep_merged_with_pins_last():
    run = _run()
    task = run["spec"]["tasks"][0]
    arm = run["spec"]["arms"][0]

    payload = build_bench_job_payload(run, task, arm, 2)

    assert payload["user_id"] == USER_ID
    assert payload["context"] == {
        "bench": {
            "run_id": RUN_ID,
            "task": "t1",
            "arm": "baseline",
            "replicate": 2,
        }
    }
    assert payload["required_deliverables"] == ["output/a.md"]
    assert payload["datasource_ids"] == []
    assert payload["config_name"] == "worker_base"
    assert payload["config_override"] == {
        "llm": {"temperature": 0.2, "model": "pinned-model"},
        "nested": {
            "task": True,
            "arm": True,
            "winner": "arm",
        },
        "autonomy": "full",
    }


def test_seeded_queue_is_stable_and_skips_ledger_entries():
    run = _run(replicates=2)
    first = build_submission_queue(run)
    assert build_submission_queue(run) == first
    assert len(first) == 6

    task, arm, replicate = first[0]
    run["state"] = [
        {
            "task": task["id"],
            "arm": arm["name"],
            "replicate": replicate,
            "job_id": "already-created",
            "final_status": "completed",
        }
    ]
    remaining = build_submission_queue(run)
    assert len(remaining) == 5
    assert (task, arm, replicate) not in remaining


@pytest.mark.asyncio
async def test_sweeper_tops_up_only_to_max_in_flight():
    store = FakeStore(_run(max_in_flight=2))
    create_job = AsyncMock(
        side_effect=[
            {"id": "job-1", "status": "created"},
            {"id": "job-2", "status": "created"},
        ]
    )

    created = await sweep_run(store, store.run, create_job)

    assert created == 2
    assert create_job.await_count == 2
    assert len(store.run["state"]) == 2
    assert all(entry["final_status"] is None for entry in store.run["state"])
    assert store.run["status"] == "running"


@pytest.mark.asyncio
async def test_sweeper_refreshes_terminal_status_before_top_up():
    state = [
        {
            "task": "t1",
            "arm": "baseline",
            "replicate": 1,
            "job_id": "job-done",
            "final_status": None,
        },
        {
            "task": "t2",
            "arm": "baseline",
            "replicate": 1,
            "job_id": "job-live",
            "final_status": None,
        },
    ]
    run = _run(state=state, max_in_flight=2)
    store = FakeStore(
        run,
        [_tagged(state[0], "failed"), _tagged(state[1], "processing")],
    )
    create_job = AsyncMock(return_value={"id": "job-new", "status": "created"})

    assert await sweep_run(store, store.run, create_job) == 1
    assert store.run["state"][0]["final_status"] == "failed"
    assert store.run["state"][1]["final_status"] is None
    assert store.run["state"][2]["job_id"] == "job-new"


@pytest.mark.asyncio
async def test_restart_resumes_from_persisted_ledger_without_duplicate():
    first_store = FakeStore(_run(max_in_flight=1))
    first_create = AsyncMock(
        return_value={"id": "job-before-restart", "status": "created"}
    )
    assert await sweep_run(first_store, first_store.run, first_create) == 1
    first_entry = first_store.run["state"][0]

    # Simulated process restart: a fresh store instance sees only the durable
    # run row and authoritative tagged job status.
    restarted = FakeStore(
        first_store.run,
        [_tagged(first_entry, "completed")],
    )
    second_create = AsyncMock(
        return_value={"id": "job-after-restart", "status": "created"}
    )

    assert await sweep_run(restarted, restarted.run, second_create) == 1
    assert [entry["job_id"] for entry in restarted.run["state"]] == [
        "job-before-restart",
        "job-after-restart",
    ]
    assert restarted.run["state"][0]["final_status"] == "completed"


@pytest.mark.asyncio
async def test_restart_reconstructs_job_created_before_ledger_commit():
    run = _run(max_in_flight=1)
    crash_window_job = {
        "id": "job-in-crash-window",
        "status": "processing",
        "created_at": datetime(2026, 8, 4, tzinfo=timezone.utc),
        "context": {
            "bench": {
                "run_id": RUN_ID,
                "task": "t2",
                "arm": "baseline",
                "replicate": 1,
            }
        },
    }
    store = FakeStore(run, [crash_window_job])
    create_job = AsyncMock()

    assert await sweep_run(store, store.run, create_job) == 0
    create_job.assert_not_awaited()
    assert store.run["state"][0]["job_id"] == "job-in-crash-window"
