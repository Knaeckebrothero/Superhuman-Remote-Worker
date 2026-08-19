"""Synthetic Job Bench request/archive metrics and aggregate hygiene tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from services.bench import (
    analyze_job_metrics,
    build_aggregates,
    fetch_main_requests,
    fetch_phase_events,
)


def _request(
    when: datetime,
    *,
    input_tokens: int = 100,
    output_tokens: int = 10,
    latency_ms: int = 10,
    cache_read_tokens: int = 0,
) -> dict:
    return {
        "ts": when,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "latency_ms": latency_ms,
        "cache_read_tokens": cache_read_tokens,
    }


def test_final_strategic_wrap_and_tail_anomaly_are_attributed():
    start = datetime(2026, 8, 4, 10, 0, tzinfo=timezone.utc)
    requests = [_request(start + timedelta(seconds=i)) for i in range(10)]

    row = analyze_job_metrics(
        job_id="job-1",
        status="completed",
        requests=requests,
        phase_events=[
            ("S", start + timedelta(seconds=1)),
            ("T", start + timedelta(seconds=7)),
        ],
    )

    # S closes requests 0..1, T closes 2..7, and the implicit final S wrap
    # owns 8..9. Without the final-wrap rule this would incorrectly be 20%.
    assert row["strategic_share_latency_pct"] == 40.0
    assert row["strategic_share_prompt_tokens_pct"] == 40.0
    assert row["strategic_share_requests_pct"] == 40.0
    assert row["tail_requests"] == 2
    assert row["tail_anomaly"] is True  # 2/10 > 15%
    assert row["request_count"] == 10
    assert row["input_tokens"] == 1000
    assert row["output_tokens"] == 100
    assert row["median_prompt_tokens"] == 100


def test_cache_metrics_separate_a_warm_handoff_from_a_cold_one():
    """Cold calls, not the hit ratio, are the load-bearing lane signal.

    A rotation that dropped the provider prefix cache would re-pay full input
    on the first call of each batch. Two jobs can post a similar hit ratio
    while one of them went cold at a boundary, so the count is reported
    alongside the ratio rather than folded into it.
    """

    start = datetime(2026, 8, 4, 10, 0, tzinfo=timezone.utc)
    warm = analyze_job_metrics(
        job_id="warm",
        status="completed",
        requests=[
            _request(
                start + timedelta(seconds=i), input_tokens=100, cache_read_tokens=80
            )
            for i in range(4)
        ],
        phase_events=[],
    )
    assert warm["cache_read_tokens"] == 320
    assert warm["cache_hit_pct"] == 80.0
    assert warm["cold_calls"] == 0

    cold_boundary = analyze_job_metrics(
        job_id="cold",
        status="completed",
        requests=[
            _request(start, input_tokens=100, cache_read_tokens=80),
            _request(
                start + timedelta(seconds=1), input_tokens=100, cache_read_tokens=0
            ),
            _request(
                start + timedelta(seconds=2), input_tokens=100, cache_read_tokens=80
            ),
            _request(
                start + timedelta(seconds=3), input_tokens=100, cache_read_tokens=80
            ),
        ],
        phase_events=[],
    )
    assert cold_boundary["cold_calls"] == 1
    assert cold_boundary["cache_hit_pct"] == 60.0


def test_requests_without_input_tokens_are_not_counted_cold():
    """A provider that omits cache detail must read as unknown, not as cold.

    Otherwise every job on such a provider would report 100% cold calls and
    look like a catastrophic cache regression that never happened.
    """

    start = datetime(2026, 8, 4, 10, 0, tzinfo=timezone.utc)
    row = analyze_job_metrics(
        job_id="no-usage",
        status="completed",
        requests=[
            _request(start, input_tokens=0, cache_read_tokens=0),
            _request(start + timedelta(seconds=1), input_tokens=0, cache_read_tokens=0),
        ],
        phase_events=[],
    )
    assert row["cold_calls"] == 0
    assert row["cache_hit_pct"] is None


def test_exactly_fifteen_percent_tail_is_not_an_anomaly():
    start = datetime(2026, 8, 4, 10, 0, tzinfo=timezone.utc)
    requests = [_request(start + timedelta(seconds=i)) for i in range(20)]
    row = analyze_job_metrics(
        job_id="job-2",
        status="completed",
        requests=requests,
        phase_events=[("T", start + timedelta(seconds=16))],
    )
    assert row["tail_requests"] == 3
    assert row["tail_anomaly"] is False


def test_no_first_llm_request_is_classified_as_infra():
    row = analyze_job_metrics(
        job_id="job-infra",
        status="failed",
        requests=[],
        phase_events=[],
    )
    assert row == {
        "job_id": "job-infra",
        "status": "failed",
        "classification": "infra",
        "request_count": 0,
        "n_req": 0,
        "tail_anomaly": False,
        "tail_requests": 0,
        "tail_req": 0,
    }


def test_aggregates_exclude_infra_and_tail_anomalies():
    spec = {
        "replicates": 3,
        "tasks": [{"id": "task", "family": "small"}],
        "arms": [{"name": "baseline"}],
    }
    jobs = [
        {
            "task": "task",
            "arm": "baseline",
            "status": "completed",
            "classification": "job",
            "tail_anomaly": False,
            "wall_minutes": 4,
            "request_count": 10,
            "median_prompt_tokens": 200,
            "strategic_share_latency_pct": 40,
            "strategic_share_prompt_tokens_pct": 35,
        },
        {
            "task": "task",
            "arm": "baseline",
            "status": "failed",
            "classification": "infra",
            "tail_anomaly": False,
            "request_count": 0,
        },
        {
            "task": "task",
            "arm": "baseline",
            "status": "completed",
            "classification": "job",
            "tail_anomaly": True,
            "wall_minutes": 400,
            "request_count": 1000,
            "median_prompt_tokens": 50000,
            "strategic_share_latency_pct": 99,
            "strategic_share_prompt_tokens_pct": 99,
        },
    ]

    aggregate = build_aggregates(spec, jobs)[0]
    assert aggregate["terminal_jobs"] == 3
    assert aggregate["completed_jobs"] == 2
    assert aggregate["infra_jobs"] == 1
    assert aggregate["tail_anomaly_jobs"] == 1
    assert aggregate["included_jobs"] == 1
    assert aggregate["metrics"] == {
        "wall_minutes": {"median": 4, "min": 4, "max": 4},
        "request_count": {"median": 10, "min": 10, "max": 10},
        "median_prompt_tokens": {"median": 200, "min": 200, "max": 200},
        "strategic_share_latency_pct": {
            "median": 40,
            "min": 40,
            "max": 40,
        },
        "strategic_share_prompt_tokens_pct": {
            "median": 35,
            "min": 35,
            "max": 35,
        },
        # Job rows predating cache capture carry no cache keys, and they
        # aggregate to unknown rather than to zero. Zero would read as "every
        # call missed the cache" and turn a run against a provider that never
        # reported cache detail into a phantom regression.
        "cache_hit_pct": None,
        "cold_calls": None,
    }


def test_aggregates_keep_two_arms_separate():
    spec = {
        "replicates": 1,
        "tasks": [{"id": "task"}],
        "arms": [{"name": "baseline"}, {"name": "candidate"}],
    }
    jobs = [
        {
            "task": "task",
            "arm": arm,
            "status": "completed",
            "classification": "job",
            "tail_anomaly": False,
            "wall_minutes": wall,
            "request_count": 1,
            "median_prompt_tokens": 100,
            "strategic_share_latency_pct": 50,
            "strategic_share_prompt_tokens_pct": 50,
        }
        for arm, wall in (("baseline", 10), ("candidate", 2))
    ]

    aggregates = build_aggregates(spec, jobs)

    assert [
        (row["arm"], row["metrics"]["wall_minutes"]["median"]) for row in aggregates
    ] == [
        ("baseline", 10),
        ("candidate", 2),
    ]


@pytest.mark.asyncio
async def test_audit_reader_pagination_keeps_server_latency():
    audit_reader = AsyncMock()
    audit_reader.list_llm_requests.side_effect = [
        {
            "entries": [
                {
                    "timestamp": "2026-08-04T10:00:00Z",
                    "token_usage": {
                        "prompt_tokens": 123,
                        "completion_tokens": 7,
                    },
                    "latency_ms": 456,
                }
            ],
            "hasMore": True,
        },
        {
            "entries": [
                {
                    "timestamp": "2026-08-04T10:00:01Z",
                    "token_usage": {
                        "input_tokens": 200,
                        "output_tokens": 8,
                    },
                    "latency_ms": 500,
                }
            ],
            "hasMore": False,
        },
    ]

    rows = await fetch_main_requests(audit_reader, "job-id")

    assert [(row["input_tokens"], row["latency_ms"]) for row in rows] == [
        (123, 456),
        (200, 500),
    ]
    assert (
        audit_reader.list_llm_requests.await_args_list[0].kwargs["call_type"] == "main"
    )
    assert audit_reader.list_llm_requests.await_args_list[1].kwargs["offset"] == 1


@pytest.mark.asyncio
async def test_phase_events_use_archive_listing_and_filter_inherited_files():
    gitea = AsyncMock()
    gitea.is_initialized = True
    gitea.list_contents.return_value = [
        {
            "name": "todos_phase_1_strategic_20260804_100000.md",
            "path": "archive/todos_phase_1_strategic_20260804_100000.md",
            "type": "file",
        },
        {
            "name": "todos_phase_2_tactical_20260804_101000.md",
            "path": "archive/todos_phase_2_tactical_20260804_101000.md",
            "type": "file",
        },
    ]
    resolve_repo = AsyncMock(return_value=("job-repo", "job/branch"))

    events = await fetch_phase_events(
        gitea,
        resolve_repo,
        "job-id",
        not_before=datetime(2026, 8, 4, 10, 5, tzinfo=timezone.utc),
    )

    assert events == [("T", datetime(2026, 8, 4, 10, 10, tzinfo=timezone.utc))]
    resolve_repo.assert_awaited_once_with("job-id")
    gitea.list_contents.assert_awaited_once_with(
        "job-repo", "archive", ref="job/branch"
    )
