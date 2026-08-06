"""Revision, transaction, and bootstrap tests for the typed daily rollup."""

from __future__ import annotations

import asyncio
import copy
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest

from orchestrator.services.infrastructure_metering.rollup import (
    ApplyDisposition,
    AuditDaySnapshot,
    BootstrapStatus,
    BootstrapState,
    BootstrapStepResult,
    DailyUsageRow,
    RollupPassResult,
    RollupContractError,
    TypedUsageDailyRollup,
    legacy_dimensions,
    typed_usage_rollup_loop,
)


def _aggregate_row(
    *,
    quantity: str = "1",
    cost_usd: str | None = None,
    priced_quantity: str = "0",
    unpriced_quantity: str = "1",
    priced_events: int = 0,
    unpriced_events: int = 1,
    resource: str = "workspace_pod",
    has_infrastructure_v2: bool = False,
) -> dict[str, Any]:
    return {
        "user_id": None,
        "project_id": None,
        "category": "compute",
        "resource": resource,
        "unit": "vcpu-hour",
        "measurement_basis": "scheduler-request",
        "resource_class": "kubernetes-pod",
        "attribution_scope": "unknown",
        "cost_domain": "workload-allocation",
        "measurement_algorithm": "legacy-end-stamped-v1",
        "quantity": Decimal(quantity),
        "cost_usd": Decimal(cost_usd) if cost_usd is not None else None,
        "priced_quantity": Decimal(priced_quantity),
        "unpriced_quantity": Decimal(unpriced_quantity),
        "priced_events": priced_events,
        "unpriced_events": unpriced_events,
        "events": priced_events + unpriced_events,
        "has_infrastructure_v2": has_infrastructure_v2,
    }


class _Acquire:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, *_args):
        return False


class _AuditTransaction:
    def __init__(self, connection, options):
        self.connection = connection
        self.options = options

    async def __aenter__(self):
        self.connection.pool.transaction_options.append(self.options)
        self.connection.snapshot_dirty = copy.deepcopy(self.connection.pool.dirty)
        self.connection.snapshot_rows = copy.deepcopy(self.connection.pool.rows)
        return self

    async def __aexit__(self, *_args):
        self.connection.snapshot_dirty = None
        self.connection.snapshot_rows = None
        return False


class _AuditConnection:
    def __init__(self, pool):
        self.pool = pool
        self.snapshot_dirty = None
        self.snapshot_rows = None

    def transaction(self, **options):
        return _AuditTransaction(self, options)

    def _dirty(self):
        return (
            self.snapshot_dirty if self.snapshot_dirty is not None else self.pool.dirty
        )

    def _rows(self):
        return self.snapshot_rows if self.snapshot_rows is not None else self.pool.rows

    async def fetch(self, sql, *args):
        if "typed-rollup:seed-event-free-dirty" in sql:
            inserted = []
            for day in args[0]:
                if day not in self.pool.dirty:
                    self.pool.dirty[day] = 1
                    inserted.append({"day": day})
            return inserted
        if "typed-rollup:pending-dirty" in sql:
            days, revisions, through_day, limit = args
            applied = dict(zip(days, revisions))
            result = [
                {"day": day, "revision": revision}
                for day, revision in sorted(self._dirty().items())
                if day <= through_day and revision > applied.get(day, 0)
            ][:limit]
            if self.pool.after_pending is not None:
                callback = self.pool.after_pending
                self.pool.after_pending = None
                callback()
            return result
        if "typed-rollup:day-aggregate" in sql:
            day = args[0].date()
            return copy.deepcopy(self._rows().get(day, []))
        if "typed-rollup:dirty-after" in sql:
            through_day, cursor, limit = args
            return [
                {"day": day, "revision": revision}
                for day, revision in sorted(self._dirty().items())
                if day <= through_day and (cursor is None or day > cursor)
            ][:limit]
        raise AssertionError(f"unexpected audit fetch: {sql}")

    async def fetchrow(self, sql, *args):
        if "typed-rollup:dirty-revision" in sql:
            revision = self._dirty().get(args[0])
            return None if revision is None else {"revision": revision}
        if "typed-rollup:seed-dirty" in sql:
            through_day = args[0].date()
            inserted = 0
            retained = [day for day in self.pool.rows if day <= through_day]
            for day in retained:
                if day not in self.pool.dirty:
                    self.pool.dirty[day] = 1
                    inserted += 1
            return {
                "inserted_days": inserted,
                "last_retained_day": max(retained, default=None),
            }
        rows = await self.fetch(sql, *args)
        return rows[0] if rows else None


class _AuditPool:
    def __init__(self, rows=None, dirty=None):
        self.rows = copy.deepcopy(rows or {})
        self.dirty = dict(dirty or {})
        self.transaction_options: list[dict[str, Any]] = []
        self.after_pending = None

    def acquire(self):
        return _Acquire(_AuditConnection(self))

    async def fetch(self, sql, *args):
        return await _AuditConnection(self).fetch(sql, *args)

    async def fetchrow(self, sql, *args):
        return await _AuditConnection(self).fetchrow(sql, *args)


class _AppTransaction:
    def __init__(self, connection):
        self.connection = connection
        self.before = None

    async def __aenter__(self):
        pool = self.connection.pool
        self.before = copy.deepcopy(
            (
                pool.applied,
                pool.daily,
                pool.coverage,
                pool.watermark,
                pool.bootstrap,
                pool.delete_count,
            )
        )
        return self

    async def __aexit__(self, exc_type, *_args):
        if exc_type is not None:
            pool = self.connection.pool
            (
                pool.applied,
                pool.daily,
                pool.coverage,
                pool.watermark,
                pool.bootstrap,
                pool.delete_count,
            ) = self.before
        return False


class _AppConnection:
    def __init__(self, pool):
        self.pool = pool

    def transaction(self, **_options):
        return _AppTransaction(self)

    async def fetch(self, sql, *args):
        if "typed-rollup:applied-revisions" in sql:
            through_day = args[0]
            return [
                {"day": day, "applied_audit_revision": state["revision"]}
                for day, state in sorted(self.pool.applied.items())
                if day <= through_day
            ]
        if "typed-rollup:stale-coverage-revisions" in sql:
            through_day, limit = args
            if self.pool.cutover_day is None:
                return []
            return [
                {"day": day}
                for day, coverage in sorted(self.pool.coverage.items())
                if day <= through_day
                and day >= self.pool.cutover_day
                and coverage["state"] == "sealed"
                and day in self.pool.applied
                and self.pool.applied[day].get("infra_coverage_revision")
                != coverage.get("coverage_revision")
            ][:limit]
        if "typed-rollup:missing-rollup-days" in sql:
            through_day, limit = args
            if self.pool.cutover_day is None:
                return []
            return [
                {"day": day}
                for day, coverage in sorted(self.pool.coverage.items())
                if day <= through_day
                and day >= self.pool.cutover_day
                and coverage["state"] == "sealed"
                and day not in self.pool.applied
            ][:limit]
        if "typed-rollup:app-day-rows" in sql:
            return copy.deepcopy(self.pool.daily.get(args[0], []))
        raise AssertionError(f"unexpected app fetch: {sql}")

    async def fetchrow(self, sql, *args):
        if "typed-rollup:bootstrap-state" in sql:
            row = copy.deepcopy(self.pool.bootstrap)
            row["last_closed_day"] = (
                self.pool.watermark if self.pool.watermark_row_exists else None
            )
            return row
        if "typed-rollup:watermark-for-update" in sql:
            if not self.pool.watermark_row_exists:
                return None
            return {"last_closed_day": self.pool.watermark}
        if "typed-rollup:day-coverage" in sql:
            return copy.deepcopy(self.pool.coverage.get(args[0]))
        if "typed-rollup:app-day-state" in sql:
            state = self.pool.applied.get(args[0])
            return (
                None if state is None else {"applied_audit_revision": state["revision"]}
            )
        rows = await self.fetch(sql, *args)
        return rows[0] if rows else None

    async def fetchval(self, sql, *args):
        if "typed-rollup:cas-day-state" in sql:
            (
                day,
                revision,
                coverage_status,
                unknown_ranges,
                infra_coverage_revision,
            ) = args
            current = self.pool.applied.get(day)
            if current is not None:
                if current["revision"] > revision or (
                    current["revision"] == revision
                    and current.get("infra_coverage_revision")
                    == infra_coverage_revision
                ):
                    return None
            self.pool.applied[day] = {
                "revision": revision,
                "coverage_status": coverage_status,
                "unknown_ranges": json.loads(unknown_ranges),
                "infra_coverage_revision": infra_coverage_revision,
            }
            return revision
        if "typed-rollup:advance-watermark" in sql:
            day = args[0]
            if not self.pool.watermark_row_exists:
                return None
            if self.pool.watermark is None or self.pool.watermark < day:
                self.pool.watermark = day
                return day
            return None
        if "typed-rollup:first-unsealed" in sql:
            current, through_day = args
            blocked = [
                day
                for day, state in self.pool.coverage.items()
                if (current is None or day > current)
                and day <= through_day
                and state["state"] != "sealed"
            ]
            if self.pool.cutover_day is not None:
                candidate = max(
                    self.pool.cutover_day,
                    self.pool.cutover_day
                    if current is None
                    else current + timedelta(days=1),
                )
                while candidate <= through_day:
                    if self.pool.coverage.get(candidate, {}).get("state") != "sealed":
                        blocked.append(candidate)
                        break
                    candidate += timedelta(days=1)
            return min(blocked, default=None)
        if "typed-rollup:bootstrap-reconciled" in sql:
            day, expected = args
            if (
                self.pool.bootstrap["status"] != "reconciling"
                or self.pool.bootstrap["reconciled_through_day"] != expected
            ):
                return None
            self.pool.bootstrap["reconciled_through_day"] = day
            return day
        if "typed-rollup:bootstrap-complete" in sql:
            seeded_through = args[0]
            if (
                self.pool.bootstrap["status"] != "reconciling"
                or self.pool.bootstrap["seeded_through_day"] != seeded_through
            ):
                return None
            self.pool.bootstrap.update(
                {
                    "status": "complete",
                    "reconciled_through_day": seeded_through,
                    "completed_at": datetime.now(timezone.utc),
                    "sanitized_error": None,
                }
            )
            return "complete"
        if "typed-rollup:bootstrap-start" in sql:
            if self.pool.bootstrap["status"] not in {"pending", "error"}:
                return None
            self.pool.bootstrap.update(
                {
                    "status": "running",
                    "seeded_through_day": None,
                    "reconciled_through_day": None,
                    "started_at": self.pool.bootstrap["started_at"]
                    or datetime.now(timezone.utc),
                    "completed_at": None,
                    "sanitized_error": None,
                }
            )
            return "running"
        if "typed-rollup:bootstrap-seeded" in sql:
            if (
                self.pool.bootstrap["status"] != "running"
                or self.pool.bootstrap["seeded_through_day"] is not None
            ):
                return None
            self.pool.bootstrap.update(
                {
                    "status": "reconciling",
                    "seeded_through_day": args[0],
                    "reconciled_through_day": None,
                    "completed_at": None,
                    "sanitized_error": None,
                }
            )
            return args[0]
        raise AssertionError(f"unexpected app fetchval: {sql}")

    async def execute(self, sql, *args):
        if "typed-rollup:delete-day" in sql:
            self.pool.delete_count += 1
            self.pool.daily[args[0]] = []
            return "DELETE"
        if "typed-rollup:bootstrap-error" in sql:
            if self.pool.bootstrap["status"] != "complete":
                self.pool.bootstrap.update(
                    {"status": "error", "sanitized_error": json.loads(args[0])}
                )
            return "UPDATE"
        raise AssertionError(f"unexpected app execute: {sql}")

    async def executemany(self, sql, rows):
        if "typed-rollup:insert-day-row" not in sql:
            raise AssertionError(f"unexpected app executemany: {sql}")
        for values in rows:
            if self.pool.fail_insert:
                raise RuntimeError("injected write failure containing secret")
            (
                day,
                user_id,
                project_id,
                category,
                resource,
                unit,
                measurement_basis,
                resource_class,
                attribution_scope,
                cost_domain,
                measurement_algorithm,
                quantity,
                cost_usd,
                priced_quantity,
                unpriced_quantity,
                priced_events,
                unpriced_events,
                events,
            ) = values
            self.pool.daily.setdefault(day, []).append(
                {
                    "user_id": user_id,
                    "project_id": project_id,
                    "category": category,
                    "resource": resource,
                    "unit": unit,
                    "measurement_basis": measurement_basis,
                    "resource_class": resource_class,
                    "attribution_scope": attribution_scope,
                    "cost_domain": cost_domain,
                    "measurement_algorithm": measurement_algorithm,
                    "quantity": quantity,
                    "cost_usd": cost_usd,
                    "priced_quantity": priced_quantity,
                    "unpriced_quantity": unpriced_quantity,
                    "priced_events": priced_events,
                    "unpriced_events": unpriced_events,
                    "events": events,
                }
            )


class _AppPool:
    def __init__(self):
        self.applied: dict[date, dict[str, Any]] = {}
        self.daily: dict[date, list[dict[str, Any]]] = {}
        self.coverage: dict[date, dict[str, Any]] = {}
        self.watermark: date | None = None
        self.watermark_row_exists = True
        self.cutover_day: date | None = None
        self.bootstrap = {
            "status": "pending",
            "seeded_through_day": None,
            "reconciled_through_day": None,
            "started_at": None,
            "completed_at": None,
            "sanitized_error": None,
        }
        self.delete_count = 0
        self.fail_insert = False

    def acquire(self):
        return _Acquire(_AppConnection(self))

    async def fetch(self, sql, *args):
        return await _AppConnection(self).fetch(sql, *args)

    async def fetchrow(self, sql, *args):
        return await _AppConnection(self).fetchrow(sql, *args)

    async def fetchval(self, sql, *args):
        return await _AppConnection(self).fetchval(sql, *args)

    async def execute(self, sql, *args):
        return await _AppConnection(self).execute(sql, *args)


def test_legacy_dimension_adapter_is_source_specific():
    llm = legacy_dimensions(
        {"category": "llm", "resource": "model-x", "user_id": "user-1"}
    )
    assert llm == {
        "measurement_basis": "api-consumed",
        "cost_domain": "external-service",
        "resource_class": "llm-model",
        "attribution_scope": "customer",
        "measurement_algorithm": "legacy-point-v1",
    }

    workspace = legacy_dimensions({"category": "compute", "resource": "workspace_pod"})
    assert workspace["measurement_basis"] == "scheduler-request"
    assert workspace["cost_domain"] == "workload-allocation"
    assert workspace["resource_class"] == "kubernetes-pod"
    assert workspace["attribution_scope"] == "unknown"
    assert workspace["measurement_algorithm"] == "legacy-end-stamped-v1"


def test_daily_row_preserves_free_unpriced_and_partial_coverage_exactly():
    free = DailyUsageRow.from_record(
        _aggregate_row(
            quantity="4",
            cost_usd="0",
            priced_quantity="4",
            unpriced_quantity="0",
            priced_events=1,
            unpriced_events=0,
        )
    )
    assert free.cost_usd == Decimal(0)
    assert free.priced_quantity == Decimal(4)

    unpriced = DailyUsageRow.from_record(
        _aggregate_row(quantity="4", unpriced_quantity="4")
    )
    assert unpriced.cost_usd is None
    assert unpriced.unpriced_events == 1

    partial = DailyUsageRow.from_record(
        _aggregate_row(
            quantity="4",
            cost_usd="1.25",
            priced_quantity="2",
            unpriced_quantity="2",
            priced_events=1,
            unpriced_events=1,
        )
    )
    assert partial.cost_usd == Decimal("1.25")
    assert partial.events == 2


def test_daily_row_uses_exact_numeric_38_arithmetic_and_rejects_float():
    row = _aggregate_row(
        quantity="100000000000000000000.000000000000000000",
        cost_usd="0",
        priced_quantity="99999999999999999999.999999999999999999",
        unpriced_quantity="0.000000000000000001",
        priced_events=1,
        unpriced_events=1,
    )
    assert DailyUsageRow.from_record(row).quantity == Decimal(
        "100000000000000000000.000000000000000000"
    )
    row["quantity"] = 0.1
    with pytest.raises(RollupContractError, match="exact decimal"):
        DailyUsageRow.from_record(row)


def test_bootstrap_readiness_requires_complete_matching_state_and_watermark():
    day = date(2026, 8, 1)
    complete_at = datetime(2026, 8, 2, tzinfo=timezone.utc)
    ready = BootstrapState(
        BootstrapStatus.COMPLETE,
        seeded_through_day=day,
        reconciled_through_day=day,
        watermark=day,
        completed_at=complete_at,
    )
    assert ready.read_ready

    assert not BootstrapState(BootstrapStatus.COMPLETE).read_ready
    assert not BootstrapState(
        BootstrapStatus.COMPLETE,
        seeded_through_day=day,
        reconciled_through_day=day - timedelta(days=1),
        watermark=day,
        completed_at=complete_at,
    ).read_ready
    assert not BootstrapState(
        BootstrapStatus.COMPLETE,
        seeded_through_day=day,
        reconciled_through_day=day,
        watermark=None,
        completed_at=complete_at,
    ).read_ready


@pytest.mark.asyncio
async def test_repeatable_read_race_leaves_higher_revision_for_next_pass():
    day = date(2026, 8, 1)
    audit = _AuditPool({day: [_aggregate_row()]}, {day: 1})
    app = _AppPool()
    rollup = TypedUsageDailyRollup(audit, app)

    def commit_late_event():
        audit.dirty[day] = 2
        audit.rows[day] = [
            _aggregate_row(quantity="2", unpriced_quantity="2", unpriced_events=2)
        ]

    audit.after_pending = commit_late_event
    first = await rollup.run_pass(through_day=day, limit=1)

    assert first.applied == 1
    assert app.applied[day]["revision"] == 1
    assert app.applied[day]["infra_coverage_revision"] is None
    assert app.daily[day][0]["quantity"] == Decimal(1)
    assert audit.transaction_options[0] == {
        "isolation": "repeatable_read",
        "readonly": True,
    }

    second = await rollup.run_pass(through_day=day, limit=1)
    assert second.applied == 1
    assert app.applied[day]["revision"] == 2
    assert app.daily[day][0]["quantity"] == Decimal(2)


@pytest.mark.asyncio
async def test_stale_app_revision_cas_cannot_delete_newer_rows():
    day = date(2026, 8, 1)
    audit = _AuditPool()
    app = _AppPool()
    app.applied[day] = {
        "revision": 2,
        "coverage_status": "complete",
        "unknown_ranges": [],
    }
    newer = _aggregate_row(quantity="2", unpriced_quantity="2")
    app.daily[day] = [copy.deepcopy(newer)]
    result = await TypedUsageDailyRollup(audit, app)._apply_snapshot(
        AuditDaySnapshot(day, 1, (DailyUsageRow.from_record(_aggregate_row()),))
    )

    assert result.disposition is ApplyDisposition.STALE
    assert app.delete_count == 0
    assert app.applied[day]["revision"] == 2
    assert app.daily[day] == [newer]


@pytest.mark.asyncio
async def test_replacement_and_revision_claim_rollback_together():
    day = date(2026, 8, 1)
    audit = _AuditPool()
    app = _AppPool()
    old = _aggregate_row(quantity="3", unpriced_quantity="3")
    app.applied[day] = {
        "revision": 1,
        "coverage_status": "complete",
        "unknown_ranges": [],
    }
    app.daily[day] = [copy.deepcopy(old)]
    app.watermark = day
    app.fail_insert = True

    with pytest.raises(RuntimeError, match="injected write failure"):
        await TypedUsageDailyRollup(audit, app)._apply_snapshot(
            AuditDaySnapshot(
                day,
                2,
                (
                    DailyUsageRow.from_record(
                        _aggregate_row(quantity="4", unpriced_quantity="4")
                    ),
                ),
            )
        )

    assert app.applied[day]["revision"] == 1
    assert app.daily[day] == [old]
    assert app.watermark == day
    assert app.delete_count == 0


@pytest.mark.asyncio
async def test_newer_revision_full_replaces_rows_that_disappeared():
    day = date(2026, 8, 1)
    remaining = _aggregate_row(quantity="2", unpriced_quantity="2")
    removed = _aggregate_row(
        quantity="8",
        unpriced_quantity="8",
        resource="old-resource",
    )
    audit = _AuditPool({day: [remaining]}, {day: 2})
    app = _AppPool()
    app.applied[day] = {
        "revision": 1,
        "coverage_status": "complete",
        "unknown_ranges": [],
    }
    app.daily[day] = [copy.deepcopy(remaining), copy.deepcopy(removed)]

    result = await TypedUsageDailyRollup(audit, app).run_pass(through_day=day, limit=1)
    assert result.applied == 1
    assert len(app.daily[day]) == 1
    assert app.daily[day][0]["resource"] == "workspace_pod"


@pytest.mark.asyncio
async def test_infrastructure_day_requires_an_explicit_seal():
    day = date(2026, 8, 1)
    audit = _AuditPool()
    app = _AppPool()
    snapshot = AuditDaySnapshot(
        day,
        1,
        (DailyUsageRow.from_record(_aggregate_row(has_infrastructure_v2=True)),),
        requires_infrastructure_seal=True,
    )
    result = await TypedUsageDailyRollup(audit, app)._apply_snapshot(snapshot)
    assert result.disposition is ApplyDisposition.UNSEALED
    assert app.applied == {}
    assert app.watermark is None

    app.coverage[day] = {
        "state": "sealed",
        "coverage_status": "partial",
        "coverage_revision": "coverage-revision-1",
        "unknown_ranges": [{"start": "2026-08-01T01:00:00Z"}],
    }
    result = await TypedUsageDailyRollup(audit, app)._apply_snapshot(snapshot)
    assert result.disposition is ApplyDisposition.APPLIED
    assert app.applied[day]["coverage_status"] == "partial"
    assert app.applied[day]["unknown_ranges"] == app.coverage[day]["unknown_ranges"]
    assert app.applied[day]["infra_coverage_revision"] == "coverage-revision-1"
    assert app.watermark == day


@pytest.mark.asyncio
async def test_sealed_day_without_a_coverage_revision_fails_closed():
    day = date(2026, 8, 1)
    app = _AppPool()
    app.coverage[day] = {
        "state": "sealed",
        "coverage_status": "complete",
        "coverage_revision": None,
        "unknown_ranges": [],
    }
    snapshot = AuditDaySnapshot(
        day,
        1,
        (DailyUsageRow.from_record(_aggregate_row(has_infrastructure_v2=True)),),
        requires_infrastructure_seal=True,
    )

    with pytest.raises(RollupContractError, match="no coverage revision"):
        await TypedUsageDailyRollup(_AuditPool(), app)._apply_snapshot(snapshot)

    assert app.applied == {}
    assert app.daily == {}


@pytest.mark.asyncio
async def test_coverage_only_revision_change_reapplies_same_audit_revision():
    day = date(2026, 8, 1)
    audit = _AuditPool(
        {day: [_aggregate_row(has_infrastructure_v2=True)]},
        {day: 1},
    )
    app = _AppPool()
    app.cutover_day = day
    app.coverage[day] = {
        "state": "sealed",
        "coverage_status": "complete",
        "coverage_revision": "coverage-revision-1",
        "unknown_ranges": [],
    }
    rollup = TypedUsageDailyRollup(audit, app)

    first = await rollup.run_pass(through_day=day, limit=1)
    assert first.applied == 1
    assert app.applied[day]["revision"] == 1
    assert app.applied[day]["infra_coverage_revision"] == "coverage-revision-1"

    app.coverage[day] = {
        "state": "sealed",
        "coverage_status": "partial",
        "coverage_revision": "coverage-revision-2",
        "unknown_ranges": [{"start": "2026-08-01T12:00:00Z"}],
    }
    second = await rollup.run_pass(through_day=day, limit=1)

    assert second.selected == 1
    assert second.applied == 1
    assert second.stale == 0
    assert app.applied[day] == {
        "revision": 1,
        "coverage_status": "partial",
        "unknown_ranges": [{"start": "2026-08-01T12:00:00Z"}],
        "infra_coverage_revision": "coverage-revision-2",
    }
    assert len(app.daily[day]) == 1


@pytest.mark.asyncio
async def test_watermark_does_not_cross_an_earlier_unsealed_day():
    blocked = date(2026, 8, 1)
    day = date(2026, 8, 2)
    audit = _AuditPool({day: [_aggregate_row()]}, {day: 1})
    app = _AppPool()
    app.coverage[blocked] = {
        "state": "open",
        "coverage_status": None,
        "unknown_ranges": [],
    }

    result = await TypedUsageDailyRollup(audit, app).run_pass(through_day=day, limit=4)
    assert result.applied == 1
    assert result.blocked_day == blocked
    assert app.applied[day]["revision"] == 1
    assert app.watermark is None


@pytest.mark.asyncio
async def test_pass_is_bounded_and_processes_oldest_dirty_days_first():
    days = [date(2026, 8, offset) for offset in (1, 2, 3)]
    audit = _AuditPool(
        {day: [_aggregate_row()] for day in days},
        {day: 1 for day in reversed(days)},
    )
    app = _AppPool()

    result = await TypedUsageDailyRollup(audit, app).run_pass(
        through_day=days[-1], limit=2
    )
    assert result.selected == 2
    assert result.applied == 2
    assert set(app.applied) == set(days[:2])
    assert days[2] not in app.daily


@pytest.mark.asyncio
async def test_event_free_pass_advances_only_when_no_infra_seal_is_required():
    through = date(2026, 8, 2)
    audit = _AuditPool()
    app = _AppPool()

    result = await TypedUsageDailyRollup(audit, app).run_pass(through_day=through)
    assert result.watermark == through
    assert app.watermark == through

    next_day = through + timedelta(days=1)
    app.cutover_day = next_day
    result = await TypedUsageDailyRollup(audit, app).run_pass(through_day=next_day)
    assert result.blocked_day == next_day
    assert app.watermark == through


@pytest.mark.asyncio
async def test_event_free_sealed_day_is_seeded_and_rolls_exact_coverage_revision():
    day = date(2026, 8, 3)
    audit = _AuditPool()
    app = _AppPool()
    app.cutover_day = day
    app.coverage[day] = {
        "state": "sealed",
        "coverage_status": "partial",
        "coverage_revision": "event-free-coverage-revision",
        "unknown_ranges": [{"start": "2026-08-03T12:00:00Z"}],
    }

    result = await TypedUsageDailyRollup(audit, app).run_pass(
        through_day=day,
        limit=1,
    )

    assert result.selected == 1
    assert result.applied == 1
    assert result.rows == 0
    assert audit.dirty[day] == 1
    assert app.applied[day] == {
        "revision": 1,
        "coverage_status": "partial",
        "unknown_ranges": [{"start": "2026-08-03T12:00:00Z"}],
        "infra_coverage_revision": "event-free-coverage-revision",
    }
    assert app.daily[day] == []
    assert app.watermark == day


@pytest.mark.asyncio
async def test_event_free_day_seeding_remains_bounded_and_pending():
    first_day = date(2026, 8, 3)
    second_day = first_day + timedelta(days=1)
    audit = _AuditPool()
    app = _AppPool()
    app.cutover_day = first_day
    for day in (first_day, second_day):
        app.coverage[day] = {
            "state": "sealed",
            "coverage_status": "complete",
            "coverage_revision": f"coverage-{day.isoformat()}",
            "unknown_ranges": [],
        }
    rollup = TypedUsageDailyRollup(audit, app)

    first = await rollup.run_pass(through_day=second_day, limit=1)
    assert first.selected == first.applied == 1
    assert set(app.applied) == {first_day}
    assert set(audit.dirty) == {first_day}
    assert app.watermark == first_day

    second = await rollup.run_pass(through_day=second_day, limit=1)
    assert second.selected == second.applied == 1
    assert set(app.applied) == {first_day, second_day}
    assert set(audit.dirty) == {first_day, second_day}
    assert app.watermark == second_day


@pytest.mark.asyncio
async def test_missing_watermark_seed_fails_closed_and_rolls_back_day_write():
    day = date(2026, 8, 1)
    audit = _AuditPool({day: [_aggregate_row()]}, {day: 1})
    app = _AppPool()
    app.watermark_row_exists = False

    result = await TypedUsageDailyRollup(audit, app).run_pass(through_day=day, limit=1)

    assert result.error_code == "rollup-pass-RollupContractError"
    assert app.applied == {}
    assert app.daily == {}


@pytest.mark.asyncio
async def test_bootstrap_seeds_rebuilds_reconciles_and_completes():
    days = [date(2026, 8, 1), date(2026, 8, 2)]
    audit = _AuditPool(
        {
            days[0]: [_aggregate_row(quantity="4", unpriced_quantity="4")],
            days[1]: [
                _aggregate_row(
                    quantity="4",
                    cost_usd="0",
                    priced_quantity="4",
                    unpriced_quantity="0",
                    priced_events=1,
                    unpriced_events=0,
                )
            ],
        }
    )
    app = _AppPool()

    result = await TypedUsageDailyRollup(audit, app).run_bootstrap_step(
        through_day=days[-1], limit=8
    )

    assert result.status is BootstrapStatus.COMPLETE
    assert result.seeded_through_day == days[-1]
    assert result.reconciled_through_day == days[-1]
    assert app.bootstrap["status"] == "complete"
    assert app.watermark == days[-1]
    assert app.daily[days[0]][0]["cost_usd"] is None
    assert app.daily[days[0]][0]["priced_quantity"] == Decimal(0)
    assert app.daily[days[0]][0]["unpriced_quantity"] == Decimal(4)
    assert app.daily[days[0]][0]["unpriced_events"] == 1
    assert app.daily[days[1]][0]["cost_usd"] == Decimal(0)
    assert app.daily[days[1]][0]["priced_quantity"] == Decimal(4)
    assert app.daily[days[1]][0]["unpriced_quantity"] == Decimal(0)
    assert app.daily[days[1]][0]["priced_events"] == 1


@pytest.mark.asyncio
async def test_empty_bootstrap_cannot_cross_an_unsealed_infrastructure_day():
    blocked = date(2026, 8, 1)
    through = date(2026, 8, 2)
    audit = _AuditPool()
    app = _AppPool()
    app.coverage[blocked] = {
        "state": "sealing",
        "coverage_status": None,
        "unknown_ranges": [],
    }

    result = await TypedUsageDailyRollup(audit, app).run_bootstrap_step(
        through_day=through
    )
    assert result.status is BootstrapStatus.RECONCILING
    assert result.blocked_day == blocked
    assert app.bootstrap["status"] == "reconciling"
    assert app.watermark is None


@pytest.mark.asyncio
async def test_empty_bootstrap_requires_day_seals_after_cutover():
    cutover = date(2026, 8, 1)
    through = date(2026, 8, 2)
    audit = _AuditPool()
    app = _AppPool()
    app.cutover_day = cutover

    result = await TypedUsageDailyRollup(audit, app).run_bootstrap_step(
        through_day=through
    )
    assert result.status is BootstrapStatus.RECONCILING
    assert result.blocked_day == cutover
    assert app.watermark is None

    app.coverage[cutover] = {
        "state": "sealed",
        "coverage_status": "complete",
        "coverage_revision": "cutover-coverage-revision",
        "unknown_ranges": [],
    }
    app.coverage[through] = {
        "state": "sealed",
        "coverage_status": "complete",
        "coverage_revision": "through-coverage-revision",
        "unknown_ranges": [],
    }
    result = await TypedUsageDailyRollup(audit, app).run_bootstrap_step(
        through_day=through
    )
    assert result.status is BootstrapStatus.COMPLETE
    assert app.watermark == through


@pytest.mark.asyncio
async def test_bootstrap_error_is_sanitized_and_non_load_bearing():
    day = date(2026, 8, 1)
    audit = _AuditPool({day: [_aggregate_row()]}, {day: 1})
    app = _AppPool()
    app.bootstrap.update({"status": "reconciling", "seeded_through_day": day})
    app.fail_insert = True

    result = await TypedUsageDailyRollup(audit, app).run_bootstrap_step(through_day=day)
    assert result.status is BootstrapStatus.ERROR
    serialized = json.dumps(app.bootstrap["sanitized_error"])
    assert "secret" not in serialized
    assert app.bootstrap["sanitized_error"]["exception_type"] == "RuntimeError"


@pytest.mark.asyncio
async def test_delayed_bootstrap_error_cannot_downgrade_complete_state():
    day = date(2026, 8, 1)
    app = _AppPool()
    app.watermark = day
    app.bootstrap.update(
        {
            "status": "complete",
            "seeded_through_day": day,
            "reconciled_through_day": day,
            "completed_at": datetime.now(timezone.utc),
            "sanitized_error": None,
        }
    )
    rollup = TypedUsageDailyRollup(_AuditPool(), app)

    await rollup._record_bootstrap_error(RuntimeError("late failure"))
    await rollup._seed_bootstrap(day + timedelta(days=1))

    assert app.bootstrap["status"] == "complete"
    assert app.bootstrap["seeded_through_day"] == day
    assert app.bootstrap["sanitized_error"] is None


@pytest.mark.asyncio
async def test_loop_survives_an_iteration_failure():
    shutdown = asyncio.Event()

    class ExplodingRollup:
        is_available = True

        async def run_cycle(self, **_kwargs):
            shutdown.set()
            raise RuntimeError("boom")

    await typed_usage_rollup_loop(
        shutdown,
        ExplodingRollup(),  # type: ignore[arg-type]
        interval_s=0.01,
    )
    assert shutdown.is_set()


@pytest.mark.asyncio
async def test_loop_drains_progressing_bootstrap_without_steady_state_sleep():
    shutdown = asyncio.Event()

    class CatchingUpRollup:
        is_available = True
        calls = 0

        async def run_cycle(self, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return BootstrapStepResult(
                    True,
                    BootstrapStatus.RECONCILING,
                    rollup=RollupPassResult(True, selected=32, applied=32),
                )
            shutdown.set()
            return BootstrapStepResult(True, BootstrapStatus.COMPLETE)

    rollup = CatchingUpRollup()
    await asyncio.wait_for(
        typed_usage_rollup_loop(
            shutdown,
            rollup,  # type: ignore[arg-type]
            interval_s=60,
            catchup_delay_s=0,
        ),
        timeout=1,
    )
    assert rollup.calls == 2
