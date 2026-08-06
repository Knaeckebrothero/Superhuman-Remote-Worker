"""Coverage-gap waiver and sealed-day degradation contract tests."""

from __future__ import annotations

import copy
import json
from datetime import date, datetime, timezone
from typing import Any
from uuid import UUID

import pytest

from orchestrator.services.infrastructure_metering.coverage import (
    CoverageGapConflict,
    CoverageGapContractError,
    CoverageGapNotFound,
    CoverageGapWaiverService,
    degrade_sealed_days_for_gap,
)


UTC = timezone.utc
GAP_ID = UUID("10000000-0000-0000-0000-000000000001")
ACTOR_ID = UUID("20000000-0000-0000-0000-000000000002")
REQUEST_ID = UUID("30000000-0000-0000-0000-000000000003")
SCOPE_EPOCH_ID = UUID("40000000-0000-0000-0000-000000000004")
TX_TIME = datetime(2026, 8, 7, 12, tzinfo=UTC)
DAY_ONE = date(2026, 8, 5)
DAY_TWO = date(2026, 8, 6)
GAP_START = datetime(2026, 8, 5, 23, 30, tzinfo=UTC)
GAP_END = datetime(2026, 8, 6, 0, 30, tzinfo=UTC)


def _timestamp(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _gap(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": GAP_ID,
        "scope_epoch_id": SCOPE_EPOCH_ID,
        "gap_start": GAP_START,
        "gap_end": GAP_END,
        "resolution": "unresolved",
        "resolution_details": {"collector_evidence": {"watch": "disconnected"}},
        "resolved_at": None,
        "resolved_by": None,
    }
    row.update(overrides)
    return row


def _day(
    day: date,
    *,
    status: str = "complete",
    revision: str = "seal-v1:original",
    sequence: int = 1,
    unknown_ranges: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "day": day,
        "state": "sealed",
        "coverage_status": status,
        "coverage_revision": revision,
        "coverage_sequence": sequence,
        "unknown_ranges": copy.deepcopy(unknown_ranges or []),
    }


class _Acquire:
    def __init__(self, connection: _Connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, *_args: Any):
        return False


class _Transaction:
    def __init__(self, connection: _Connection):
        self.connection = connection
        self.before: (
            tuple[dict[UUID, dict[str, Any]], dict[date, dict[str, Any]]] | None
        ) = None

    async def __aenter__(self):
        assert not self.connection.in_transaction
        self.connection.in_transaction = True
        self.before = copy.deepcopy(
            (self.connection.pool.gaps, self.connection.pool.days)
        )
        return self

    async def __aexit__(self, exc_type, *_args: Any):
        if exc_type is not None:
            assert self.before is not None
            self.connection.pool.gaps, self.connection.pool.days = self.before
        self.connection.in_transaction = False
        return False


class _Connection:
    def __init__(self, pool: _Pool):
        self.pool = pool
        self.in_transaction = False
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def transaction(self):
        return _Transaction(self)

    def _record(self, sql: str, args: tuple[Any, ...]) -> None:
        assert self.in_transaction
        self.calls.append((sql, args))

    async def fetchrow(self, sql: str, *args: Any):
        self._record(sql, args)
        if "infra-coverage:lock-gap" in sql:
            row = self.pool.gaps.get(args[0])
            if row is None:
                return None
            return {**copy.deepcopy(row), "transaction_time": TX_TIME}
        if "infra-coverage:waive-gap" in sql:
            gap_id, raw_details, resolved_at, actor_id = args
            row = self.pool.gaps.get(gap_id)
            if row is None or row["resolution"] != "unresolved":
                return None
            row.update(
                {
                    "resolution": "waived",
                    "resolution_details": json.loads(raw_details),
                    "resolved_at": resolved_at,
                    "resolved_by": actor_id,
                }
            )
            return {"resolved_at": resolved_at}
        if "infra-coverage:degrade-day" in sql:
            if self.pool.fail_degrade:
                raise RuntimeError("injected sealed-day write failure")
            day, revision, sequence, raw_ranges, old_sequence, old_revision = args
            row = self.pool.days.get(day)
            if (
                row is None
                or row["state"] != "sealed"
                or row["coverage_sequence"] != old_sequence
                or row["coverage_revision"] != old_revision
            ):
                return None
            row.update(
                {
                    "coverage_status": "partial",
                    "coverage_revision": revision,
                    "coverage_sequence": sequence,
                    "unknown_ranges": json.loads(raw_ranges),
                }
            )
            return {
                "coverage_sequence": sequence,
                "coverage_revision": revision,
            }
        raise AssertionError(f"unexpected fetchrow: {sql}")

    async def fetch(self, sql: str, *args: Any):
        self._record(sql, args)
        if "infra-coverage:sealed-days" not in sql:
            raise AssertionError(f"unexpected fetch: {sql}")
        first_day, last_day = args
        return [
            {**copy.deepcopy(row), "cutover_at": self.pool.cutover_at}
            for day, row in sorted(self.pool.days.items())
            if first_day <= day <= last_day and row["state"] == "sealed"
        ]


class _Pool:
    def __init__(
        self,
        *,
        gaps: list[dict[str, Any]] | None = None,
        days: list[dict[str, Any]] | None = None,
        cutover_at: datetime = datetime(2026, 8, 5, 23, 45, tzinfo=UTC),
    ):
        self.gaps = {row["id"]: copy.deepcopy(row) for row in (gaps or [])}
        self.days = {row["day"]: copy.deepcopy(row) for row in (days or [])}
        self.cutover_at = cutover_at
        self.fail_degrade = False
        self.connection = _Connection(self)

    def acquire(self):
        return _Acquire(self.connection)


@pytest.mark.asyncio
async def test_waiver_merges_provenance_and_degrades_each_intersecting_day_once():
    existing = {
        "start": _timestamp(datetime(2026, 8, 6, 2, tzinfo=UTC)),
        "end": _timestamp(datetime(2026, 8, 6, 3, tzinfo=UTC)),
        "evidence": "existing-seal-proof",
    }
    pool = _Pool(
        gaps=[_gap()],
        days=[
            _day(DAY_ONE),
            _day(
                DAY_TWO,
                status="partial",
                revision="coverage-gap-waiver-v1:older",
                sequence=3,
                unknown_ranges=[existing],
            ),
        ],
    )
    service = CoverageGapWaiverService(pool)  # type: ignore[arg-type]

    first = await service.waive(
        GAP_ID,
        ACTOR_ID,
        "  inventory history cannot be recovered  ",
        REQUEST_ID,
    )

    assert first.replayed is False
    assert first.resolved_at == TX_TIME
    assert [item.day for item in first.degraded_days] == [DAY_ONE, DAY_TWO]
    gap = pool.gaps[GAP_ID]
    assert gap["resolution"] == "waived"
    assert gap["resolved_by"] == ACTOR_ID
    assert gap["resolution_details"]["collector_evidence"] == {"watch": "disconnected"}
    assert gap["resolution_details"]["waiver"] == {
        "actor_id": str(ACTOR_ID),
        "reason": "inventory history cannot be recovered",
        "idempotency_key": str(REQUEST_ID),
        "resolved_at": _timestamp(TX_TIME),
    }

    day_one = pool.days[DAY_ONE]
    day_two = pool.days[DAY_TWO]
    assert day_one["coverage_status"] == day_two["coverage_status"] == "partial"
    assert day_one["coverage_sequence"] == 2
    assert day_two["coverage_sequence"] == 4
    assert day_one["unknown_ranges"] == [
        {
            "start": _timestamp(pool.cutover_at),
            "end": _timestamp(datetime(2026, 8, 6, tzinfo=UTC)),
        }
    ]
    assert day_two["unknown_ranges"][0] == existing
    assert day_two["unknown_ranges"][1] == {
        "start": _timestamp(datetime(2026, 8, 6, tzinfo=UTC)),
        "end": _timestamp(GAP_END),
    }
    assert day_one["coverage_revision"].startswith("coverage-gap-waiver-v1:")
    assert day_two["coverage_revision"].startswith("coverage-gap-waiver-v1:")

    sequences = {day: row["coverage_sequence"] for day, row in pool.days.items()}
    revisions = {day: row["coverage_revision"] for day, row in pool.days.items()}
    replay = await service.waive(
        GAP_ID,
        ACTOR_ID,
        "inventory history cannot be recovered",
        REQUEST_ID,
    )

    assert replay.replayed is True
    assert replay.degraded_days == ()
    assert sequences == {
        day: row["coverage_sequence"] for day, row in pool.days.items()
    }
    assert revisions == {
        day: row["coverage_revision"] for day, row in pool.days.items()
    }


@pytest.mark.asyncio
async def test_same_key_with_different_request_and_other_resolutions_are_rejected():
    pool = _Pool(gaps=[_gap()], days=[])
    service = CoverageGapWaiverService(pool)  # type: ignore[arg-type]
    await service.waive(GAP_ID, ACTOR_ID, "approved gap", REQUEST_ID)

    with pytest.raises(CoverageGapConflict, match="different request"):
        await service.waive(GAP_ID, ACTOR_ID, "different reason", REQUEST_ID)
    with pytest.raises(CoverageGapConflict, match="different request"):
        await service.waive(
            GAP_ID,
            ACTOR_ID,
            "approved gap",
            UUID("50000000-0000-0000-0000-000000000005"),
        )

    backfilled_id = UUID("60000000-0000-0000-0000-000000000006")
    pool.gaps[backfilled_id] = _gap(
        id=backfilled_id,
        resolution="backfilled",
        resolved_at=TX_TIME,
    )
    with pytest.raises(CoverageGapConflict, match="different request"):
        await service.waive(backfilled_id, ACTOR_ID, "waive anyway", REQUEST_ID)


@pytest.mark.asyncio
async def test_open_ended_and_missing_gaps_fail_closed():
    pool = _Pool(gaps=[_gap(gap_end=None)])
    service = CoverageGapWaiverService(pool)  # type: ignore[arg-type]

    with pytest.raises(CoverageGapContractError, match="open-ended"):
        await service.waive(GAP_ID, ACTOR_ID, "approved", REQUEST_ID)
    with pytest.raises(CoverageGapNotFound):
        await service.waive(
            UUID("70000000-0000-0000-0000-000000000007"),
            ACTOR_ID,
            "approved",
            REQUEST_ID,
        )


@pytest.mark.asyncio
async def test_waiver_reason_rejects_control_characters_before_database_io():
    pool = _Pool(gaps=[_gap()])

    with pytest.raises(CoverageGapContractError, match="printable"):
        await CoverageGapWaiverService(pool).waive(  # type: ignore[arg-type]
            GAP_ID,
            ACTOR_ID,
            "approved\nfor billing",
            REQUEST_ID,
        )

    assert pool.connection.calls == []


@pytest.mark.asyncio
async def test_gap_and_day_degradation_roll_back_together():
    pool = _Pool(gaps=[_gap()], days=[_day(DAY_ONE)])
    before_gap = copy.deepcopy(pool.gaps[GAP_ID])
    before_day = copy.deepcopy(pool.days[DAY_ONE])
    pool.fail_degrade = True

    with pytest.raises(RuntimeError, match="injected sealed-day"):
        await CoverageGapWaiverService(pool).waive(  # type: ignore[arg-type]
            GAP_ID,
            ACTOR_ID,
            "approved",
            REQUEST_ID,
        )

    assert pool.gaps[GAP_ID] == before_gap
    assert pool.days[DAY_ONE] == before_day


@pytest.mark.asyncio
async def test_degradation_helper_is_union_idempotent_and_revision_deterministic():
    covered_ranges = [
        {
            "start": _timestamp(datetime(2026, 8, 5, 23, 45, tzinfo=UTC)),
            "end": _timestamp(datetime(2026, 8, 6, tzinfo=UTC)),
        },
        {
            "start": _timestamp(datetime(2026, 8, 6, tzinfo=UTC)),
            "end": _timestamp(GAP_END),
        },
    ]
    covered = _Pool(
        days=[
            _day(
                DAY_ONE,
                status="partial",
                sequence=2,
                unknown_ranges=covered_ranges,
            )
        ]
    )
    async with covered.acquire() as conn:
        async with conn.transaction():
            result = await degrade_sealed_days_for_gap(
                conn,
                gap_id=GAP_ID,
                idempotency_key=REQUEST_ID,
                gap_start=GAP_START,
                gap_end=GAP_END,
            )
    assert result == ()
    assert covered.days[DAY_ONE]["coverage_sequence"] == 2

    inconsistent_complete = _Pool(
        days=[_day(DAY_ONE, unknown_ranges=[covered_ranges[0]])]
    )
    async with inconsistent_complete.acquire() as conn:
        async with conn.transaction():
            repaired = await degrade_sealed_days_for_gap(
                conn,
                gap_id=GAP_ID,
                idempotency_key=REQUEST_ID,
                gap_start=GAP_START,
                gap_end=GAP_END,
            )
    assert len(repaired) == 1
    assert inconsistent_complete.days[DAY_ONE]["coverage_status"] == "partial"
    assert inconsistent_complete.days[DAY_ONE]["unknown_ranges"] == [covered_ranges[0]]

    left = _Pool(days=[_day(DAY_ONE)])
    right = _Pool(days=[_day(DAY_ONE)])
    revisions = []
    for pool in (left, right):
        async with pool.acquire() as conn:
            async with conn.transaction():
                changed = await degrade_sealed_days_for_gap(
                    conn,
                    gap_id=GAP_ID,
                    idempotency_key=REQUEST_ID,
                    gap_start=GAP_START,
                    gap_end=GAP_END,
                )
        revisions.append(changed[0].coverage_revision)

    assert revisions[0] == revisions[1]
