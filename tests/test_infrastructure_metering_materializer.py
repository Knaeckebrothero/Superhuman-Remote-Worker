"""Deterministic segmentation and strict cross-database publication tests."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest

from orchestrator.services.infrastructure_metering.materializer import (
    FrozenPublicationPlan,
    InfrastructureUsageMaterializer,
    PublicationConflictError,
    PublicationContractError,
    PublicationDisabledError,
    PublicationFenceError,
    build_usage_plan,
)
from orchestrator.services.usage_ledger import (
    StrictUsageConflict,
    StrictUsagePartitionMissing,
    UsageLedger,
    UsageRates,
)


UTC = timezone.utc
START = datetime(2026, 8, 5, 23, 30, tzinfo=UTC)
INTERVAL_ID = UUID("10000000-0000-0000-0000-000000000001")
LIFECYCLE_ID = UUID("20000000-0000-0000-0000-000000000002")
OWNER_ID = UUID("30000000-0000-0000-0000-000000000003")
USER_ID = UUID("40000000-0000-0000-0000-000000000004")
PROJECT_ID = UUID("50000000-0000-0000-0000-000000000005")
PLAN_ID = UUID("60000000-0000-0000-0000-000000000006")


def _interval(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": INTERVAL_ID,
        "source_cluster": "main-dev",
        "source_kind": "pod",
        "source_uid": "pod-uid-1",
        "source_lifecycle_id": LIFECYCLE_ID,
        "revision_no": 1,
        "source_revision": "a" * 64,
        "namespace": "srw",
        "name": "workspace-pod-1",
        "category": "compute",
        "resource": "workspace_pod",
        "measurement_basis": "scheduler-request",
        "cost_domain": "workload-allocation",
        "resource_class": "kubernetes-pod",
        "attribution_scope": "customer",
        "owner_kind": "job",
        "owner_id": str(OWNER_ID),
        "user_id": USER_ID,
        "project_id": PROJECT_ID,
        "attribution_source": "job-label-db",
        "attribution_quality": "exact",
        "backing_resource_uid": None,
        "lifecycle_confidence": "kubernetes-visible",
        "cpu_millicores": 8000,
        "memory_bytes": 16 * 1024**3,
        "storage_bytes": None,
        "capacity_source": "pod-requests-v1",
        "capacity_quality": "exact",
        "measurement_algorithm": "kubernetes-pod-requests-v1",
        "started_at": START,
        "start_time_source": "kubernetes-creation",
        "start_uncertainty_us": 0,
        "ended_at": START + timedelta(hours=1, minutes=30),
        "end_time_source": "complete-list-absence",
        "end_uncertainty_us": 1_000_000,
        "last_seen_at": START + timedelta(hours=1),
        "last_confirmed_at": START + timedelta(hours=1, minutes=30),
        "materialized_through": START,
        "end_reason": "absent-complete-list",
    }
    row.update(overrides)
    return row


def _rate(
    rate_id: str,
    unit: str,
    price: str,
    effective_from: datetime,
    effective_to: datetime | None = None,
) -> dict[str, Any]:
    return {
        "id": UUID(rate_id),
        "unit": unit,
        "usd_per_unit": Decimal(price),
        "effective_from": effective_from,
        "effective_to": effective_to,
    }


def _plan(**kwargs: Any):
    plan = build_usage_plan(
        _interval(**kwargs), (), creator_generation=7, plan_id=PLAN_ID
    )
    assert plan is not None
    return plan


def test_compute_plan_splits_at_utc_midnight_and_multiplies_capacity() -> None:
    plan = _plan()

    assert plan.period_start == START
    assert plan.period_end == datetime(2026, 8, 6, tzinfo=UTC)
    rows = {item.event.payload["unit"]: item.event.payload for item in plan.events}
    assert rows["vcpu-hour"]["quantity"] == "4"
    assert rows["gib-hour"]["quantity"] == "8"
    assert rows["vcpu-hour"]["source_capacity_value"] == "8000"
    assert rows["gib-hour"]["source_capacity_value"] == str(16 * 1024**3)
    assert rows["vcpu-hour"]["rate_usd"] is None
    assert rows["vcpu-hour"]["cost_usd"] is None
    assert rows["vcpu-hour"]["period_start"].endswith(".000000Z")


def test_plan_splits_on_any_unit_rate_boundary_and_snapshots_free_rate() -> None:
    boundary = START + timedelta(minutes=15)
    rates = (
        _rate(
            "70000000-0000-0000-0000-000000000007",
            "vcpu-hour",
            "0.10",
            START - timedelta(days=1),
            boundary,
        ),
        _rate(
            "71000000-0000-0000-0000-000000000007",
            "vcpu-hour",
            "0.20",
            boundary,
        ),
        _rate(
            "72000000-0000-0000-0000-000000000007",
            "gib-hour",
            "0",
            START - timedelta(days=1),
        ),
    )

    plan = build_usage_plan(_interval(), rates, creator_generation=7, plan_id=PLAN_ID)
    assert plan is not None
    assert plan.period_end == boundary
    rows = {item.event.payload["unit"]: item.event.payload for item in plan.events}
    assert rows["vcpu-hour"]["quantity"] == "2"
    assert rows["vcpu-hour"]["rate_usd"] == "0.1"
    assert rows["vcpu-hour"]["cost_usd"] == "0.2"
    assert rows["gib-hour"]["quantity"] == "4"
    assert rows["gib-hour"]["rate_usd"] == "0"
    assert rows["gib-hour"]["cost_usd"] == "0"


def test_open_interval_publishes_only_complete_confirmed_utc_days() -> None:
    open_row = _interval(
        started_at=datetime(2026, 8, 5, tzinfo=UTC),
        materialized_through=datetime(2026, 8, 5, tzinfo=UTC),
        ended_at=None,
        end_time_source=None,
        end_uncertainty_us=None,
        end_reason=None,
        last_confirmed_at=datetime(2026, 8, 6, 12, tzinfo=UTC),
    )
    plan = build_usage_plan(open_row, (), creator_generation=7)
    assert plan is not None
    assert plan.period_end == datetime(2026, 8, 6, tzinfo=UTC)

    open_row["materialized_through"] = datetime(2026, 8, 6, tzinfo=UTC)
    assert build_usage_plan(open_row, (), creator_generation=7) is None


def test_pvc_records_capacity_and_occurrence_hours_separately() -> None:
    plan = build_usage_plan(
        _interval(
            source_kind="pvc",
            category="storage",
            resource="workspace_claim",
            measurement_basis="claim-requested",
            resource_class="persistent-volume-claim",
            cpu_millicores=None,
            memory_bytes=None,
            storage_bytes=4 * 1024**3,
            materialized_through=datetime(2026, 8, 5, 10, tzinfo=UTC),
            started_at=datetime(2026, 8, 5, 10, tzinfo=UTC),
            ended_at=datetime(2026, 8, 5, 12, tzinfo=UTC),
            last_seen_at=datetime(2026, 8, 5, 12, tzinfo=UTC),
            last_confirmed_at=datetime(2026, 8, 5, 12, tzinfo=UTC),
        ),
        (),
        creator_generation=7,
    )
    assert plan is not None
    rows = {item.event.payload["unit"]: item.event.payload for item in plan.events}
    assert rows["gib-hour"]["quantity"] == "8"
    assert rows["claim-hour"]["quantity"] == "2"
    assert rows["claim-hour"]["source_capacity_value"] == "1"


def test_plan_hashes_do_not_depend_on_random_plan_identity() -> None:
    first = _plan()
    second = build_usage_plan(
        _interval(),
        (),
        creator_generation=7,
        plan_id=UUID("61000000-0000-0000-0000-000000000006"),
    )
    assert second is not None
    assert first.event_set_hash == second.event_set_hash
    assert first.rate_selection_hash == second.rate_selection_hash
    assert [item.event.row_hash for item in first.events] == [
        item.event.row_hash for item in second.events
    ]


def test_loaded_plan_recomputes_payload_hash_instead_of_trusting_jsonb() -> None:
    plan = _plan()
    rows = _event_rows(plan)
    rows[0]["event_payload"]["quantity"] = "999"

    with pytest.raises(PublicationContractError, match="payload hash mismatch"):
        FrozenPublicationPlan.from_records(_plan_row(plan), rows)


class _Acquire:
    def __init__(self, connection: Any):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, *_args: Any):
        return False


class _AuditTransaction:
    def __init__(self, connection: _AuditConnection):
        self.connection = connection
        self.before: dict[tuple[str, str, str, str], str] | None = None

    async def __aenter__(self):
        self.before = copy.deepcopy(self.connection.pool.rows)
        return self

    async def __aexit__(self, exc_type: Any, *_args: Any):
        if exc_type is not None:
            self.connection.pool.rows = self.before or {}
        return False


class _AuditConnection:
    def __init__(self, pool: _AuditPool):
        self.pool = pool

    def transaction(self, **_kwargs: Any):
        return _AuditTransaction(self)

    async def fetch(self, sql: str, *args: Any):
        if "strict-usage:attached-partitions" in sql:
            return [{"relname": name} for name in args[0] if name in self.pool.attached]
        if "strict-usage:verify-frozen" in sql:
            expected = json.loads(args[0])
            return [
                {
                    "source": item["source"],
                    "source_id": item["source_id"],
                    "unit": item["unit"],
                    "ts": item["ts"],
                    "expected_hash": item["payload_hash"],
                    "actual_hash": self.pool.rows.get(
                        (
                            item["source"],
                            item["source_id"],
                            item["unit"],
                            item["ts"],
                        )
                    ),
                }
                for item in expected
            ]
        raise AssertionError(f"unexpected audit fetch: {sql}")

    async def execute(self, sql: str, *args: Any):
        if "strict-usage:insert-frozen" not in sql:
            raise AssertionError(f"unexpected audit execute: {sql}")
        self.pool.insert_calls += 1
        inserted = 0
        for payload in json.loads(args[0]):
            key = (
                payload["source"],
                payload["source_id"],
                payload["unit"],
                payload["ts"],
            )
            if key not in self.pool.rows:
                self.pool.rows[key] = payload["payload_hash"]
                inserted += 1
        return f"INSERT 0 {inserted}"


class _AuditPool:
    def __init__(self, *, attached: set[str] | None = None):
        self.attached = attached or {"usage_events_p2026_08"}
        self.rows: dict[tuple[str, str, str, str], str] = {}
        self.insert_calls = 0

    def acquire(self):
        return _Acquire(_AuditConnection(self))


@pytest.mark.asyncio
async def test_strict_ledger_inserts_then_verifies_exact_replay() -> None:
    plan = _plan()
    audit = _AuditPool()
    ledger = UsageLedger(audit, UsageRates(None))  # type: ignore[arg-type]
    events = [item.event for item in plan.events]

    first = await ledger.publish_frozen_events(events)
    replay = await ledger.publish_frozen_events(events)

    assert (first.expected, first.inserted, first.verified) == (2, 2, 2)
    assert (replay.expected, replay.inserted, replay.verified) == (2, 0, 2)
    assert audit.insert_calls == 2
    assert len(audit.rows) == 2


@pytest.mark.asyncio
async def test_strict_ledger_rolls_back_batch_on_hash_conflict() -> None:
    plan = _plan()
    audit = _AuditPool()
    first = plan.events[0].event
    audit.rows[first.dedupe_key] = "f" * 64
    before = copy.deepcopy(audit.rows)
    ledger = UsageLedger(audit, UsageRates(None))  # type: ignore[arg-type]

    with pytest.raises(StrictUsageConflict, match="hash mismatch"):
        await ledger.publish_frozen_events([item.event for item in plan.events])

    assert audit.rows == before


@pytest.mark.asyncio
async def test_strict_ledger_refuses_missing_partition_before_insert() -> None:
    plan = _plan()
    audit = _AuditPool(attached={"usage_events_p2026_07"})
    ledger = UsageLedger(audit, UsageRates(None))  # type: ignore[arg-type]

    with pytest.raises(StrictUsagePartitionMissing) as raised:
        await ledger.publish_frozen_events([item.event for item in plan.events])

    assert raised.value.partitions == ("usage_events_p2026_08",)
    assert audit.insert_calls == 0


class _PlanningConnection:
    def __init__(self, pool: _PlanningPool):
        self.pool = pool

    def transaction(self, **_kwargs: Any):
        return _AppTransaction(self)  # type: ignore[arg-type]

    async def fetchrow(self, sql: str, *args: Any):
        if "infra-publication:control" in sql:
            return {
                "leader_generation": self.pool.generation,
                "cutover_state": self.pool.cutover_state,
                "cutover_at": START,
            }
        if "infra-publication:day-state" in sql:
            return {"state": self.pool.day_state}
        raise AssertionError(f"unexpected planning fetchrow: {sql}")

    async def fetch(self, sql: str, *args: Any):
        if "infra-publication:candidates" in sql:
            self.pool.candidate_args = args
            return [copy.deepcopy(self.pool.interval)]
        if "infra-publication:rates" in sql:
            self.pool.rate_args = args
            return copy.deepcopy(self.pool.rates)
        raise AssertionError(f"unexpected planning fetch: {sql}")

    async def execute(self, sql: str, *args: Any):
        if "infra-publication:insert-plan-events" in sql:
            self.pool.inserted_events = json.loads(args[0])
            return f"INSERT 0 {len(self.pool.inserted_events)}"
        if "infra-publication:insert-plan" in sql:
            self.pool.inserted_plan = args
            return "INSERT 0 1"
        raise AssertionError(f"unexpected planning execute: {sql}")


class _PlanningPool:
    def __init__(self):
        self.interval = _interval()
        self.rates: list[dict[str, Any]] = []
        self.generation = 7
        self.cutover_state = "active"
        self.day_state = "open"
        self.candidate_args: tuple[Any, ...] | None = None
        self.rate_args: tuple[Any, ...] | None = None
        self.inserted_plan: tuple[Any, ...] | None = None
        self.inserted_events: list[dict[str, Any]] = []
        # _AppTransaction snapshots these two attributes. Planning does not
        # mutate them, but sharing the transaction fake keeps rollback behavior
        # explicit in this unit boundary.
        self.plan = None
        self.cursor = START

    def acquire(self):
        return _Acquire(_PlanningConnection(self))


@pytest.mark.asyncio
async def test_plan_batch_freezes_app_manifest_without_audit_io() -> None:
    app = _PlanningPool()
    audit = _AuditPool()
    ledger = UsageLedger(audit, UsageRates(None))  # type: ignore[arg-type]
    materializer = InfrastructureUsageMaterializer(
        app, ledger, publication_enabled=True, batch_size=17
    )  # type: ignore[arg-type]

    plans = await materializer.plan_batch(7)

    assert len(plans) == 1
    assert app.candidate_args == (["workspace_pod"], START, 17)
    assert app.rate_args is not None
    assert app.rate_args[5] == ["vcpu-hour", "gib-hour"]
    assert app.inserted_plan is not None
    assert len(app.inserted_events) == 2
    assert all(
        event["event_payload"]["payload_hash"] == event["row_hash"]
        for event in app.inserted_events
    )
    assert audit.insert_calls == 0


@pytest.mark.asyncio
async def test_plan_batch_rejects_sealed_day_before_freezing_intent() -> None:
    app = _PlanningPool()
    app.day_state = "sealed"
    ledger = UsageLedger(_AuditPool(), UsageRates(None))  # type: ignore[arg-type]
    materializer = InfrastructureUsageMaterializer(
        app, ledger, publication_enabled=True
    )  # type: ignore[arg-type]

    with pytest.raises(PublicationConflictError, match="sealed day"):
        await materializer.plan_batch(7)

    assert app.inserted_plan is None


def _plan_row(plan: Any) -> dict[str, Any]:
    return {
        "id": plan.id,
        "source_interval_id": plan.source_interval_id,
        "source_revision": plan.source_revision,
        "plan_kind": plan.plan_kind,
        "plan_revision": plan.plan_revision,
        "advances_cursor": plan.advances_cursor,
        "previous_materialized_through": plan.previous_materialized_through,
        "correction_group_id": plan.correction_group_id,
        "period_start": plan.period_start,
        "period_end": plan.period_end,
        "expected_event_count": len(plan.events),
        "payload_schema_version": plan.payload_schema_version,
        "event_set_hash": plan.event_set_hash,
        "rate_selection_hash": plan.rate_selection_hash,
        "creator_generation": plan.creator_generation,
        "state": plan.state,
        "attempt_count": 0,
        "sanitized_error": None,
    }


def _event_rows(plan: Any) -> list[dict[str, Any]]:
    return [
        {
            "ordinal": item.ordinal,
            "canonical_rate_version_id": item.canonical_rate_version_id,
            "row_hash": item.event.row_hash,
            "event_payload": dict(item.event.payload),
        }
        for item in plan.events
    ]


class _AppTransaction:
    def __init__(self, connection: _AppConnection):
        self.connection = connection
        self.before: Any = None

    async def __aenter__(self):
        pool = self.connection.pool
        self.before = copy.deepcopy((pool.plan, pool.cursor))
        return self

    async def __aexit__(self, exc_type: Any, *_args: Any):
        if exc_type is not None:
            self.connection.pool.plan, self.connection.pool.cursor = self.before
        return False


class _AppConnection:
    def __init__(self, pool: _AppPool):
        self.pool = pool

    def transaction(self, **_kwargs: Any):
        return _AppTransaction(self)

    async def fetch(self, sql: str, *args: Any):
        if "infra-publication:plan-events" in sql:
            return copy.deepcopy(self.pool.events)
        raise AssertionError(f"unexpected app fetch: {sql}")

    async def fetchrow(self, sql: str, *args: Any):
        if "infra-publication:control" in sql:
            return {
                "leader_generation": self.pool.generation,
                "cutover_state": self.pool.cutover_state,
                "cutover_at": START,
            }
        if "infra-publication:pending-plan" in sql:
            return (
                copy.deepcopy(self.pool.plan)
                if self.pool.plan["state"] == "planned"
                else None
            )
        if "infra-publication:lock-plan" in sql:
            return {"state": self.pool.plan["state"]}
        if "infra-publication:advance-cursor" in sql:
            interval_id, revision, previous, target = args
            if (
                interval_id != self.pool.plan["source_interval_id"]
                or revision != self.pool.plan["source_revision"]
                or previous != self.pool.cursor
            ):
                return None
            self.pool.cursor = target
            return {"materialized_through": target}
        if "infra-publication:publish-plan" in sql:
            if self.pool.plan["state"] != "planned":
                return None
            self.pool.plan["state"] = "published"
            self.pool.plan["attempt_count"] += 1
            self.pool.plan["sanitized_error"] = None
            return {"id": args[0]}
        if "infra-publication:record-failure" in sql:
            plan_id, generation, state, raw_error = args
            if (
                plan_id != self.pool.plan["id"]
                or generation != self.pool.generation
                or self.pool.cutover_state != "active"
                or self.pool.plan["state"] != "planned"
            ):
                return None
            self.pool.plan["state"] = state
            self.pool.plan["attempt_count"] += 1
            self.pool.plan["sanitized_error"] = json.loads(raw_error)
            return {"state": state}
        raise AssertionError(f"unexpected app fetchrow: {sql}")


class _AppPool:
    def __init__(self, plan: Any):
        self.plan = _plan_row(plan)
        self.events = _event_rows(plan)
        self.cursor = plan.previous_materialized_through
        self.generation = 7
        self.cutover_state = "active"

    def acquire(self):
        return _Acquire(_AppConnection(self))


@pytest.mark.asyncio
async def test_materializer_gate_is_independent_and_off_by_default() -> None:
    plan = _plan()
    app = _AppPool(plan)
    ledger = UsageLedger(_AuditPool(), UsageRates(None))  # type: ignore[arg-type]
    materializer = InfrastructureUsageMaterializer(app, ledger)  # type: ignore[arg-type]

    with pytest.raises(PublicationDisabledError, match="runtime gate"):
        await materializer.publish_one(7)


@pytest.mark.asyncio
async def test_materializer_replays_committed_audit_batch_then_advances_once() -> None:
    plan = _plan()
    app = _AppPool(plan)
    audit = _AuditPool()
    ledger = UsageLedger(audit, UsageRates(None))  # type: ignore[arg-type]
    events = [item.event for item in plan.events]
    committed = await ledger.publish_frozen_events(events)
    assert committed.inserted == 2
    materializer = InfrastructureUsageMaterializer(
        app, ledger, publication_enabled=True
    )  # type: ignore[arg-type]

    result = await materializer.publish_one(7)

    assert result is not None
    assert result.audit.inserted == 0
    assert result.audit.verified == 2
    assert result.cursor_advanced
    assert app.cursor == plan.period_end
    assert app.plan["state"] == "published"
    assert app.plan["attempt_count"] == 1


@pytest.mark.asyncio
async def test_audit_commit_survives_failed_app_cursor_cas_for_exact_replay() -> None:
    plan = _plan()
    app = _AppPool(plan)
    app.cursor = plan.period_start + timedelta(seconds=1)
    audit = _AuditPool()
    ledger = UsageLedger(audit, UsageRates(None))  # type: ignore[arg-type]
    materializer = InfrastructureUsageMaterializer(
        app, ledger, publication_enabled=True
    )  # type: ignore[arg-type]

    with pytest.raises(PublicationConflictError, match="cursor changed"):
        await materializer.publish_one(7)

    assert len(audit.rows) == 2
    assert app.plan["state"] == "planned"
    assert app.plan["attempt_count"] == 0
    assert app.cursor == plan.period_start + timedelta(seconds=1)


@pytest.mark.asyncio
async def test_materializer_fences_stale_generation_before_audit_io() -> None:
    plan = _plan()
    app = _AppPool(plan)
    app.generation = 8
    audit = _AuditPool()
    ledger = UsageLedger(audit, UsageRates(None))  # type: ignore[arg-type]
    materializer = InfrastructureUsageMaterializer(
        app, ledger, publication_enabled=True
    )  # type: ignore[arg-type]

    with pytest.raises(PublicationFenceError, match="stale"):
        await materializer.publish_one(7)

    assert audit.insert_calls == 0
    assert app.plan["state"] == "planned"


@pytest.mark.asyncio
async def test_materializer_marks_audit_hash_conflict_terminal() -> None:
    plan = _plan()
    app = _AppPool(plan)
    audit = _AuditPool()
    audit.rows[plan.events[0].event.dedupe_key] = "f" * 64
    ledger = UsageLedger(audit, UsageRates(None))  # type: ignore[arg-type]
    materializer = InfrastructureUsageMaterializer(
        app, ledger, publication_enabled=True
    )  # type: ignore[arg-type]

    with pytest.raises(StrictUsageConflict):
        await materializer.publish_one(7)

    assert app.plan["state"] == "conflict"
    assert app.plan["attempt_count"] == 1
    assert app.plan["sanitized_error"] == {"code": "audit-payload-conflict"}
    assert app.cursor == plan.period_start


@pytest.mark.asyncio
async def test_materializer_keeps_missing_partition_plan_pending() -> None:
    plan = _plan()
    app = _AppPool(plan)
    audit = _AuditPool(attached={"usage_events_p2026_07"})
    ledger = UsageLedger(audit, UsageRates(None))  # type: ignore[arg-type]
    materializer = InfrastructureUsageMaterializer(
        app, ledger, publication_enabled=True
    )  # type: ignore[arg-type]

    with pytest.raises(StrictUsagePartitionMissing):
        await materializer.publish_one(7)

    assert app.plan["state"] == "planned"
    assert app.plan["attempt_count"] == 1
    assert app.plan["sanitized_error"] == {
        "code": "audit-partition-missing",
        "partitions": ["usage_events_p2026_08"],
    }
    assert app.cursor == plan.period_start
