"""Wire contracts for the extracted usage/statistics router.

Each test mounts the router on a bare FastAPI application with its own
dependency factory, which is the property that matters most here: every
metering collaborator is a ``None``-at-import application singleton, so a
factory that resolved once would freeze "metering is off" into the routes.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from orchestrator.routers import usage_reporting as routes
from orchestrator.services import usage_reporting as operations

NOW = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)
JOB_ID = "3f1f0b26-6b45-4d67-9b0f-2f1a5c9f8f11"


def _reports(**overrides: Any) -> operations.UsageReportingDependencies:
    base: dict[str, Any] = {
        "store": MagicMock(),
        "audit_reader": SimpleNamespace(is_available=False),
        "logger": logging.getLogger("test-usage-router"),
        "visible_project_ids": AsyncMock(return_value=[]),
        "scope_project_id": lambda _user: None,
    }
    base.update(overrides)
    return operations.UsageReportingDependencies(**base)


def _dependencies(
    *,
    reports: operations.UsageReportingDependencies | None = None,
    v2_reads_enabled: bool = False,
    user: dict[str, Any] | None = None,
    **overrides: Any,
) -> routes.UsageReportingDependencies:
    resolved = reports if reports is not None else _reports()
    fields: dict[str, Any] = {
        "store": resolved.store,
        "reports": resolved,
        "require_admin": AsyncMock(return_value={"id": "admin", "is_admin": True}),
        "metering_settings": SimpleNamespace(v2_reads_enabled=v2_reads_enabled),
        "require_approved_user": AsyncMock(
            return_value=user or {"id": JOB_ID, "is_admin": True}
        ),
        "scope_project_id": lambda _user: None,
    }
    fields.update(overrides)
    return routes.UsageReportingDependencies(**fields)


def _client(factory) -> TestClient:
    app = FastAPI()
    app.include_router(routes.router)
    app.state.usage_reporting_dependencies_factory = factory
    return TestClient(app, raise_server_exceptions=False)


class TestFactoryResolution:
    def test_every_request_re_resolves_the_factory(self):
        """The metering singletons appear during lifespan, not at import."""
        calls: list[int] = []
        first = _dependencies()
        second = _dependencies(
            reports=_reports(
                usage_ledger=SimpleNamespace(
                    is_available=True,
                    query_usage=AsyncMock(
                        return_value={"by_category": [], "total_cost_usd": 0.0}
                    ),
                )
            )
        )

        def factory():
            calls.append(len(calls))
            return first if len(calls) == 1 else second

        with _client(factory) as client:
            assert client.get("/api/usage").json()["available"] is False
            assert client.get("/api/usage").json()["available"] is True
        assert len(calls) == 2


class TestAggregateUsage:
    def test_metering_off_is_available_false_not_a_zero_cost(self):
        with _client(lambda: _dependencies()) as client:
            body = client.get("/api/usage").json()
        assert body == {
            "by_category": [],
            "total_cost_usd": 0.0,
            "cache_hit_ratio": 0.0,
            "cloud_estimates": [],
            "available": False,
        }

    def test_rollup_is_preferred_over_the_raw_ledger(self):
        ledger = SimpleNamespace(is_available=True, query_usage=AsyncMock())
        rollup = SimpleNamespace(
            usage=AsyncMock(return_value={"by_category": [], "total_cost_usd": 1.5})
        )
        deps = _dependencies(reports=_reports(usage_ledger=ledger, usage_rollup=rollup))
        with _client(lambda: deps) as client:
            body = client.get("/api/usage?days=3").json()
        assert body["available"] is True
        assert body["total_cost_usd"] == 1.5
        rollup.usage.assert_awaited_once()
        ledger.query_usage.assert_not_awaited()

    def test_a_failing_cloud_estimate_does_not_fail_the_measured_read(self):
        """Comparison prices are a planning aid; the ledger figure stands alone."""
        deps = _dependencies(
            reports=_reports(
                usage_ledger=SimpleNamespace(
                    is_available=True,
                    query_usage=AsyncMock(
                        return_value={"by_category": [], "total_cost_usd": 4.0}
                    ),
                ),
                usage_cloud_estimator=SimpleNamespace(
                    estimate=AsyncMock(side_effect=RuntimeError("rate card down"))
                ),
            )
        )
        with _client(lambda: deps) as client:
            body = client.get("/api/usage").json()
        assert body["total_cost_usd"] == 4.0
        assert body["cloud_estimates"] == []

    def test_an_unparseable_window_is_a_400_not_a_500(self):
        deps = _dependencies(
            reports=_reports(
                usage_ledger=SimpleNamespace(is_available=True, query_usage=AsyncMock())
            )
        )
        with _client(lambda: deps) as client:
            response = client.get("/api/usage?from_date=not-a-date")
        assert response.status_code == 400
        assert response.json()["detail"].startswith("invalid date")

    @pytest.mark.parametrize("path", ["/api/usage/breakdown", "/api/usage/timeseries"])
    def test_an_unknown_group_by_is_refused_before_any_read(self, path):
        ledger = SimpleNamespace(is_available=True, query_grouped=AsyncMock())
        deps = _dependencies(reports=_reports(usage_ledger=ledger))
        with _client(lambda: deps) as client:
            response = client.get(f"{path}?group_by=tenant")
        assert response.status_code == 400
        assert response.json()["detail"] == "bad group_by: tenant"
        ledger.query_grouped.assert_not_awaited()

    def test_breakdown_labels_unknown_keys_with_the_raw_key(self):
        rows = [
            {
                "key": "opus",
                "unit": "prompt-token",
                "quantity": 10.0,
                "cost_usd": 0.5,
                "events": 2,
            }
        ]
        deps = _dependencies(
            reports=_reports(
                usage_ledger=SimpleNamespace(
                    is_available=True, query_grouped=AsyncMock(return_value=rows)
                )
            )
        )
        with _client(lambda: deps) as client:
            body = client.get("/api/usage/breakdown?group_by=model").json()
        assert body["available"] is True
        assert body["rows"][0]["label"] == "opus"
        assert body["rows"][0]["events"] == 2

    def test_timeseries_orders_series_by_total_events(self):
        rows = [
            {
                "day": "2026-06-01",
                "key": "quiet",
                "tokens": 1.0,
                "cost_usd": 0.0,
                "events": 1,
            },
            {
                "day": "2026-06-01",
                "key": "busy",
                "tokens": 9.0,
                "cost_usd": 0.0,
                "events": 9,
            },
        ]
        deps = _dependencies(
            reports=_reports(
                usage_ledger=SimpleNamespace(
                    is_available=True, query_timeseries=AsyncMock(return_value=rows)
                )
            )
        )
        with _client(lambda: deps) as client:
            body = client.get("/api/usage/timeseries?group_by=model").json()
        assert [s["key"] for s in body["series"]] == ["busy", "quiet"]
        assert body["days"] == ["2026-06-01"]


class TestUsageV2:
    def test_the_gate_is_checked_before_the_caller_is_resolved(self):
        auth = AsyncMock()
        deps = _dependencies(v2_reads_enabled=False, require_approved_user=auth)
        with _client(lambda: deps) as client:
            response = client.get("/api/usage/v2")
        assert response.status_code == 404
        auth.assert_not_awaited()

    def test_a_guessed_reference_is_not_a_presence_oracle(self):
        """An inaccessible ref_id must 404, never 403 — a 403 confirms it exists."""
        deps = _dependencies(
            v2_reads_enabled=True,
            user={"id": JOB_ID, "is_admin": False},
            user_can_access_job_or_thread=AsyncMock(return_value=False),
        )
        with _client(lambda: deps) as client:
            response = client.get(f"/api/usage/v2?ref_id={JOB_ID}")
        assert response.status_code == 404
        assert response.json()["detail"] == "Usage reference not found"

    def test_a_malformed_reference_is_rejected_before_the_access_check(self):
        access = AsyncMock(return_value=True)
        deps = _dependencies(
            v2_reads_enabled=True, user_can_access_job_or_thread=access
        )
        with _client(lambda: deps) as client:
            response = client.get("/api/usage/v2?ref_id=not-a-uuid")
        assert response.status_code == 400
        access.assert_not_awaited()

    def test_non_customer_rows_stay_behind_the_fleet_admin_check(self):
        scoped_admin = {"id": JOB_ID, "is_admin": True}
        deps = _dependencies(
            v2_reads_enabled=True,
            user=scoped_admin,
            scope_project_id=lambda _user: "a-project",
        )
        with _client(lambda: deps) as client:
            response = client.get("/api/usage/v2?include_non_customer=true")
        assert response.status_code == 403
        assert response.json()["detail"] == "Fleet admin access required"

    def test_an_inverted_window_is_refused(self):
        deps = _dependencies(
            v2_reads_enabled=True,
            reports=_reports(
                infrastructure_usage_v2=SimpleNamespace(
                    is_available=True, summary=AsyncMock()
                ),
                infrastructure_usage_rollup=SimpleNamespace(
                    bootstrap_state=AsyncMock(
                        return_value=SimpleNamespace(read_ready=True)
                    )
                ),
            ),
        )
        with _client(lambda: deps) as client:
            response = client.get(
                "/api/usage/v2?from_date=2026-08-06T00:00:00Z"
                "&to_date=2026-08-05T00:00:00Z"
            )
        assert response.status_code == 400
        assert response.json()["detail"] == "usage window end must be after its start"


class TestStatistics:
    def test_session_wake_statistics_go_through_the_admin_gate(self):
        gate = AsyncMock(side_effect=HTTPException(status_code=403, detail="nope"))
        store = MagicMock()
        store.get_job_wake_stats = AsyncMock(
            side_effect=AssertionError("read past the gate")
        )
        deps = _dependencies(reports=_reports(store=store), require_admin=gate)
        with _client(lambda: deps) as client:
            response = client.get("/api/stats/session-wakes")
        assert response.status_code == 403

    def test_agent_statistics_count_only_known_states(self):
        store = MagicMock()
        store.list_agents = AsyncMock(
            return_value=[
                {"status": "ready"},
                {"status": "working"},
                {"status": "banana"},
            ]
        )
        deps = _dependencies(reports=_reports(store=store))
        with _client(lambda: deps) as client:
            body = client.get("/api/stats/agents").json()
        assert body["total"] == 3
        assert body["ready"] == 1
        assert "banana" not in body

    def test_a_store_failure_becomes_a_500_with_its_reason(self):
        store = MagicMock()
        store.get_daily_statistics = AsyncMock(side_effect=RuntimeError("pool closed"))
        deps = _dependencies(reports=_reports(store=store))
        with _client(lambda: deps) as client:
            response = client.get("/api/stats/daily")
        assert response.status_code == 500
        assert response.json()["detail"] == "pool closed"


class TestJobUsage:
    def test_the_job_gate_runs_before_any_ledger_read(self):
        ledger = SimpleNamespace(
            is_available=True,
            earliest_event_ts=AsyncMock(
                side_effect=AssertionError("ledger read past the gate")
            ),
            query_ref_usage=AsyncMock(),
        )
        deps = _dependencies(
            reports=_reports(usage_ledger=ledger),
            require_job_access=AsyncMock(
                side_effect=HTTPException(status_code=404, detail="Job not found")
            ),
        )
        with _client(lambda: deps) as client:
            response = client.get(f"/api/jobs/{JOB_ID}/usage")
        assert response.status_code == 404

    def test_a_measured_read_keeps_its_window_and_unpriced_caveat(self):
        job = {"id": JOB_ID, "status": "completed", "created_at": NOW}
        rows = [
            {
                "category": "compute",
                "resource": "workspace_pod",
                "unit": "vcpu-hour",
                "quantity": 2.0,
                "cost_usd": None,
                "events": 2,
                "priced_events": 0,
            }
        ]
        ledger = SimpleNamespace(
            is_available=True,
            earliest_event_ts=AsyncMock(return_value=NOW - timedelta(days=30)),
            query_ref_usage=AsyncMock(return_value=rows),
        )
        deps = _dependencies(
            reports=_reports(usage_ledger=ledger),
            require_job_access=AsyncMock(return_value=({"id": "u"}, job)),
        )
        with _client(lambda: deps) as client:
            body = client.get(f"/api/jobs/{JOB_ID}/usage").json()
        assert body["state"] == "measured"
        assert body["cost"]["usd"] is None
        assert body["cost"]["complete"] is False
        assert body["window"]["from"].endswith("Z")
