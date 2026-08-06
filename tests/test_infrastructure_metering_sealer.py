"""Unit tests for the generation-fenced infrastructure UTC day sealer."""

from __future__ import annotations

import copy
import json
from datetime import date, datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest

from orchestrator.services.infrastructure_metering.sealer import (
    _ITEM_BLOCKER_SQL,
    DaySealDisposition,
    DaySealingBlocked,
    DaySealingDisabled,
    DaySealingFenceError,
    InfrastructureUsageDaySealer,
)


UTC = timezone.utc
DAY = date(2026, 8, 2)
DAY_START = datetime(2026, 8, 2, tzinfo=UTC)
DAY_END = datetime(2026, 8, 3, tzinfo=UTC)
CUTOVER = datetime(2026, 8, 1, tzinfo=UTC)
OBSERVED_AT = datetime(2026, 8, 4, 12, tzinfo=UTC)
EPOCH_ID = UUID("10000000-0000-0000-0000-000000000001")
SCOPE_ID = UUID("20000000-0000-0000-0000-000000000001")


def _epoch(**overrides: Any) -> dict[str, Any]:
    row = {
        "id": EPOCH_ID,
        "scope_id": SCOPE_ID,
        "api_resource": "core/v1/pods",
        "required_from": CUTOVER,
        "reliable_from": CUTOVER - timedelta(hours=1),
        "continuous_since": CUTOVER - timedelta(hours=1),
        "complete_through": DAY_END + timedelta(hours=1),
        "retired_at": None,
    }
    row.update(overrides)
    return row


def _gap(
    *,
    start: datetime,
    end: datetime | None,
    resolution: str,
    gap_id: UUID | None = None,
) -> dict[str, Any]:
    return {
        "id": gap_id or uuid4(),
        "scope_epoch_id": EPOCH_ID,
        "gap_start": start,
        "gap_end": end,
        "resolution": resolution,
    }


def _snapshot(
    *,
    received_at: datetime,
    item_errors: list[dict[str, Any]] | None = None,
    snapshot_id: UUID | None = None,
    complete: bool = True,
    manifest_state: str = "sealed",
) -> dict[str, Any]:
    return {
        "id": snapshot_id or uuid4(),
        "scope_epoch_id": EPOCH_ID,
        "received_at": received_at,
        "item_errors": copy.deepcopy(item_errors or []),
        "complete": complete,
        "manifest_state": manifest_state,
    }


def _watch_event(
    *,
    received_at: datetime,
    valid_for_metering: bool | None = False,
    mutation_action: str = "presence-invalid",
    source_kind: str = "pod",
    source_uid: str = "pod-1",
) -> dict[str, Any]:
    return {
        "id": uuid4(),
        "scope_epoch_id": EPOCH_ID,
        "received_at": received_at,
        "valid_for_metering": valid_for_metering,
        "mutation_action": mutation_action,
        "source_kind": source_kind,
        "source_uid": source_uid,
    }


class _Acquire:
    def __init__(self, pool: _AppPool):
        self.pool = pool

    async def __aenter__(self) -> _Connection:
        self.pool.acquire_count += 1
        return _Connection(self.pool)

    async def __aexit__(self, *_args: Any) -> bool:
        return False


class _Transaction:
    def __init__(self, pool: _AppPool):
        self.pool = pool
        self.before: dict[date, dict[str, Any]] | None = None

    async def __aenter__(self) -> _Transaction:
        self.before = copy.deepcopy(self.pool.day_states)
        return self

    async def __aexit__(self, exc_type: Any, *_args: Any) -> bool:
        if exc_type is not None:
            self.pool.day_states = self.before or {}
            self.pool.rollbacks += 1
        else:
            self.pool.commits += 1
        return False


class _Connection:
    def __init__(self, pool: _AppPool):
        self.pool = pool

    def transaction(self, **_options: Any) -> _Transaction:
        return _Transaction(self.pool)

    async def fetch(self, sql: str, *_args: Any) -> list[dict[str, Any]]:
        if "infra-seal:epochs" in sql:
            return copy.deepcopy(self.pool.epochs)
        if "infra-seal:gaps" in sql:
            return copy.deepcopy(self.pool.gaps)
        raise AssertionError(f"unexpected sealer fetch: {sql}")

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        if "infra-seal:control" in sql:
            return {
                "leader_generation": self.pool.generation,
                "cutover_state": self.pool.cutover_state,
                "cutover_at": self.pool.cutover_at,
                "now": self.pool.observed_at,
            }
        if "infra-seal:sealed-day" in sql:
            state = self.pool.day_states.get(args[0])
            return (
                copy.deepcopy(state)
                if state is not None and state["state"] == "sealed"
                else None
            )
        if "infra-seal:lock-day" in sql:
            return copy.deepcopy(self.pool.day_states.get(args[0]))
        if "infra-seal:item-blocker" in sql:
            self.pool.item_query_args = args
            if self.pool.item_blocked:
                return {"id": uuid4()}
            epoch_ids, seal_start, day_end, observed_at = args
            epoch_ids = set(epoch_ids)
            missing_boundary_id: UUID | None = None
            for epoch in self.pool.epochs:
                if epoch["id"] not in epoch_ids:
                    continue
                effective_start = max(seal_start, epoch["required_from"])
                effective_end = min(
                    day_end,
                    epoch["retired_at"] or day_end,
                )
                evidence_through = min(epoch["complete_through"], observed_at)
                known_end = min(effective_end, evidence_through)
                snapshots = [
                    row
                    for row in self.pool.snapshots
                    if row["scope_epoch_id"] == epoch["id"]
                    and row["complete"] is True
                    and row["manifest_state"] in {"sealed", "items-expired"}
                ]
                baseline_at = max(
                    (
                        row["received_at"]
                        for row in snapshots
                        if row["received_at"] < effective_start
                    ),
                    default=None,
                )
                boundary_target = known_end
                boundary_at = min(
                    (
                        row["received_at"]
                        for row in snapshots
                        if boundary_target <= row["received_at"] <= evidence_through
                    ),
                    default=None,
                )
                if known_end > effective_start and boundary_at is None:
                    missing_boundary_id = missing_boundary_id or epoch["id"]
                relevant = [
                    row
                    for row in snapshots
                    if row["received_at"] == baseline_at
                    or effective_start <= row["received_at"] < known_end
                    or row["received_at"] == boundary_at
                ]
                if any(row["item_errors"] for row in relevant):
                    return {"id": relevant[0]["id"]}
                latest_prestart: dict[tuple[str, str], dict[str, Any]] = {}
                if baseline_at is not None:
                    for row in self.pool.watch_events:
                        if (
                            row["scope_epoch_id"] != epoch["id"]
                            or not baseline_at <= row["received_at"] < effective_start
                        ):
                            continue
                        key = (row["source_kind"], row["source_uid"])
                        previous = latest_prestart.get(key)
                        if (
                            previous is None
                            or previous["received_at"] < row["received_at"]
                        ):
                            latest_prestart[key] = row
                if any(
                    row["valid_for_metering"] is False
                    and row["mutation_action"] == "presence-invalid"
                    for row in latest_prestart.values()
                ):
                    return {"id": uuid4()}
                if any(
                    row["scope_epoch_id"] == epoch["id"]
                    and effective_start <= row["received_at"] < known_end
                    and row["valid_for_metering"] is False
                    and row["mutation_action"] == "presence-invalid"
                    for row in self.pool.watch_events
                ):
                    return {"id": uuid4()}
            return (
                None
                if missing_boundary_id is None
                else {
                    "blocker_kind": "inventory-boundary-evidence-missing",
                    "id": missing_boundary_id,
                }
            )
        if "infra-seal:interval-blocker" in sql:
            return {"id": uuid4()} if self.pool.interval_blocked else None
        if "infra-seal:plan-blocker" in sql:
            return {"id": uuid4()} if self.pool.plan_blocked else None
        if "infra-seal:finish" in sql:
            if self.pool.finish_failure == "raise":
                raise RuntimeError("injected finish failure")
            if (
                self.pool.finish_failure == "fence"
                or self.pool.generation != args[4]
                or self.pool.cutover_state != "active"
            ):
                return None
            day, status, revision, raw_unknown, _generation = args
            state = self.pool.day_states[day]
            if state["state"] != "sealing":
                return None
            state.update(
                {
                    "state": "sealed",
                    "coverage_status": status,
                    "coverage_revision": revision,
                    "unknown_ranges": json.loads(raw_unknown),
                }
            )
            return {
                "coverage_status": status,
                "coverage_revision": revision,
                "unknown_ranges": state["unknown_ranges"],
            }
        raise AssertionError(f"unexpected sealer fetchrow: {sql}")

    async def fetchval(self, sql: str, *args: Any) -> str | None:
        if "infra-seal:start" not in sql:
            raise AssertionError(f"unexpected sealer fetchval: {sql}")
        state = self.pool.day_states.get(args[0])
        if state is None or state["state"] != "open":
            return None
        state["state"] = "sealing"
        return "sealing"

    async def execute(self, sql: str, *args: Any) -> str:
        if "infra-seal:ensure-day" not in sql:
            raise AssertionError(f"unexpected sealer execute: {sql}")
        self.pool.day_states.setdefault(
            args[0],
            {
                "day": args[0],
                "state": "open",
                "coverage_status": None,
                "coverage_revision": None,
                "unknown_ranges": [],
            },
        )
        return "INSERT 0 1"


class _AppPool:
    def __init__(self) -> None:
        self.generation = 7
        self.cutover_state = "active"
        self.cutover_at = CUTOVER
        self.observed_at = OBSERVED_AT
        self.epochs = [_epoch()]
        self.gaps: list[dict[str, Any]] = []
        self.snapshots: list[dict[str, Any]] = [
            _snapshot(received_at=DAY_END),
        ]
        self.watch_events: list[dict[str, Any]] = []
        self.interval_blocked = False
        self.plan_blocked = False
        self.item_blocked = False
        self.finish_failure: str | None = None
        self.day_states: dict[date, dict[str, Any]] = {}
        self.acquire_count = 0
        self.commits = 0
        self.rollbacks = 0
        self.item_query_args: tuple[Any, ...] | None = None

    def acquire(self) -> _Acquire:
        return _Acquire(self)


def _sealer(pool: _AppPool, *, enabled: bool = True) -> InfrastructureUsageDaySealer:
    return InfrastructureUsageDaySealer(
        pool,  # type: ignore[arg-type]
        sealing_enabled=enabled,
    )


def test_item_blocker_sql_includes_boundary_and_invalid_watch_evidence() -> None:
    compact = " ".join(_ITEM_BLOCKER_SQL.split())

    assert "resource_inventory_snapshots" in compact
    assert "resource_inventory_watch_events" in compact
    assert "presence-invalid" in compact
    assert "boundary" in compact.lower()
    assert "$4" in compact
    assert "received_at" in compact
    assert "ORDER BY" in compact
    assert "LIMIT 1" in compact


@pytest.mark.asyncio
async def test_invalid_complete_snapshot_exactly_at_day_end_blocks_seal() -> None:
    app = _AppPool()
    app.snapshots = [
        _snapshot(
            received_at=DAY_END,
            item_errors=[{"code": "capacity-invalid"}],
        )
    ]

    with pytest.raises(DaySealingBlocked) as raised:
        await _sealer(app).seal_day(DAY, 7)

    assert raised.value.code == "invalid-inventory-items"
    assert app.day_states == {}
    assert app.item_query_args is not None
    assert app.item_query_args[3] == OBSERVED_AT


@pytest.mark.asyncio
async def test_earliest_post_boundary_snapshot_is_the_only_future_evidence() -> None:
    app = _AppPool()
    first_boundary = DAY_END + timedelta(minutes=5)
    app.epochs[0]["complete_through"] = DAY_END + timedelta(minutes=30)
    app.snapshots = [
        _snapshot(received_at=first_boundary),
        _snapshot(
            received_at=first_boundary + timedelta(minutes=1),
            item_errors=[{"code": "belongs-to-next-window"}],
        ),
    ]

    result = await _sealer(app).seal_day(DAY, 7)

    assert result.disposition is DaySealDisposition.SEALED


@pytest.mark.asyncio
async def test_all_snapshots_tied_at_first_boundary_are_evidence() -> None:
    app = _AppPool()
    boundary = DAY_END + timedelta(minutes=5)
    app.epochs[0]["complete_through"] = boundary
    app.snapshots = [
        _snapshot(received_at=boundary),
        _snapshot(
            received_at=boundary,
            item_errors=[{"code": "ambiguous-tied-manifest"}],
        ),
    ]

    with pytest.raises(DaySealingBlocked) as raised:
        await _sealer(app).seal_day(DAY, 7)

    assert raised.value.code == "invalid-inventory-items"


@pytest.mark.asyncio
async def test_snapshot_after_transaction_observed_at_is_not_evidence() -> None:
    app = _AppPool()
    app.epochs[0]["complete_through"] = OBSERVED_AT + timedelta(hours=1)
    app.snapshots = [
        _snapshot(received_at=DAY_END),
        _snapshot(
            received_at=OBSERVED_AT + timedelta(microseconds=1),
            item_errors=[{"code": "future-receipt"}],
        ),
    ]

    result = await _sealer(app).seal_day(DAY, 7)

    assert result.disposition is DaySealDisposition.SEALED
    assert app.item_query_args is not None
    assert app.item_query_args[3] == OBSERVED_AT


@pytest.mark.asyncio
async def test_claimed_known_end_without_boundary_manifest_fails_closed() -> None:
    app = _AppPool()
    app.snapshots = []

    with pytest.raises(DaySealingBlocked) as raised:
        await _sealer(app).seal_day(DAY, 7)

    assert raised.value.code == "inventory-boundary-evidence-missing"
    assert app.day_states == {}


@pytest.mark.asyncio
async def test_invalid_item_has_priority_over_missing_boundary_diagnostic() -> None:
    app = _AppPool()
    app.snapshots = [
        _snapshot(
            received_at=DAY_START + timedelta(hours=1),
            item_errors=[{"code": "capacity-invalid"}],
        )
    ]

    with pytest.raises(DaySealingBlocked) as raised:
        await _sealer(app).seal_day(DAY, 7)

    assert raised.value.code == "invalid-inventory-items"


@pytest.mark.asyncio
async def test_invalid_watch_presence_inside_day_blocks_seal() -> None:
    app = _AppPool()
    app.watch_events = [
        _watch_event(received_at=DAY_START + timedelta(hours=1)),
    ]

    with pytest.raises(DaySealingBlocked) as raised:
        await _sealer(app).seal_day(DAY, 7)

    assert raised.value.code == "invalid-inventory-items"
    assert app.day_states == {}


@pytest.mark.asyncio
async def test_latest_invalid_watch_state_before_day_blocks_seal() -> None:
    app = _AppPool()
    app.snapshots = [
        _snapshot(received_at=DAY_START - timedelta(hours=2)),
        _snapshot(received_at=DAY_END),
    ]
    app.watch_events = [
        _watch_event(received_at=DAY_START - timedelta(hours=1)),
    ]

    with pytest.raises(DaySealingBlocked) as raised:
        await _sealer(app).seal_day(DAY, 7)

    assert raised.value.code == "invalid-inventory-items"


@pytest.mark.asyncio
async def test_repaired_watch_state_before_day_does_not_block_seal() -> None:
    app = _AppPool()
    app.snapshots = [
        _snapshot(received_at=DAY_START - timedelta(hours=2)),
        _snapshot(received_at=DAY_END),
    ]
    app.watch_events = [
        _watch_event(received_at=DAY_START - timedelta(hours=1)),
        _watch_event(
            received_at=DAY_START - timedelta(minutes=30),
            valid_for_metering=True,
            mutation_action="confirm",
        ),
    ]

    result = await _sealer(app).seal_day(DAY, 7)

    assert result.disposition is DaySealDisposition.SEALED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event",
    [
        _watch_event(received_at=DAY_END),
        _watch_event(received_at=DAY_END + timedelta(microseconds=1)),
        _watch_event(
            received_at=DAY_START + timedelta(hours=1),
            valid_for_metering=True,
            mutation_action="confirm",
        ),
        _watch_event(
            received_at=DAY_START + timedelta(hours=1),
            valid_for_metering=False,
            mutation_action="not-applicable",
        ),
    ],
)
async def test_non_invalid_or_out_of_window_watch_event_does_not_block(
    event: dict[str, Any],
) -> None:
    app = _AppPool()
    app.watch_events = [event]

    result = await _sealer(app).seal_day(DAY, 7)

    assert result.disposition is DaySealDisposition.SEALED


@pytest.mark.asyncio
async def test_incomplete_and_staging_boundary_snapshots_are_not_evidence() -> None:
    app = _AppPool()
    app.snapshots = [
        _snapshot(received_at=DAY_END),
        _snapshot(
            received_at=DAY_END,
            item_errors=[{"code": "incomplete"}],
            complete=False,
        ),
        _snapshot(
            received_at=DAY_END,
            item_errors=[{"code": "staging"}],
            manifest_state="staging",
        ),
    ]

    result = await _sealer(app).seal_day(DAY, 7)

    assert result.disposition is DaySealDisposition.SEALED


@pytest.mark.asyncio
async def test_runtime_gate_and_generation_fence_mutate_nothing() -> None:
    app = _AppPool()
    with pytest.raises(DaySealingDisabled, match="gate is disabled"):
        await _sealer(app, enabled=False).seal_day(DAY, 7)
    assert app.acquire_count == 0
    assert app.day_states == {}

    with pytest.raises(DaySealingFenceError, match="generation is stale"):
        await _sealer(app).seal_day(DAY, 6)
    assert app.day_states == {}

    app.cutover_state = "preparing"
    with pytest.raises(DaySealingDisabled, match="cutover is not active"):
        await _sealer(app).seal_day(DAY, 7)
    assert app.day_states == {}


@pytest.mark.asyncio
async def test_complete_seal_is_immutable_idempotent() -> None:
    app = _AppPool()
    sealer = _sealer(app)

    first = await sealer.seal_day(DAY, 7)
    persisted = copy.deepcopy(app.day_states[DAY])
    app.epochs = []
    replay = await sealer.seal_day(DAY, 7)

    assert first.disposition is DaySealDisposition.SEALED
    assert first.coverage_status == "complete"
    assert first.unknown_ranges == ()
    assert first.required_scopes == 1
    assert first.coverage_revision.startswith("seal-v1:")
    assert len(first.coverage_revision) == len("seal-v1:") + 64
    assert replay.disposition is DaySealDisposition.ALREADY_SEALED
    assert replay.coverage_revision == first.coverage_revision
    assert replay.required_scopes is None
    assert app.day_states[DAY] == persisted


@pytest.mark.asyncio
async def test_stranded_sealing_state_is_not_treated_as_recoverable() -> None:
    app = _AppPool()
    app.day_states[DAY] = {
        "day": DAY,
        "state": "sealing",
        "coverage_status": None,
        "coverage_revision": None,
        "unknown_ranges": [],
    }

    with pytest.raises(ValueError, match="stranded in sealing state"):
        await _sealer(app).seal_day(DAY, 7)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("configure", "code"),
    [
        (lambda app: setattr(app, "epochs", []), "no-required-inventory-source"),
        (
            lambda app: app.epochs[0].update(
                {"complete_through": DAY_END - timedelta(microseconds=1)}
            ),
            "required-source-incomplete",
        ),
        (
            lambda app: app.gaps.append(
                _gap(
                    start=DAY_START + timedelta(hours=1),
                    end=DAY_START + timedelta(hours=2),
                    resolution="unresolved",
                )
            ),
            "unresolved-coverage-gap",
        ),
        (
            lambda app: setattr(app, "item_blocked", True),
            "invalid-inventory-items",
        ),
        (
            lambda app: setattr(app, "interval_blocked", True),
            "interval-materialization-incomplete",
        ),
        (
            lambda app: setattr(app, "plan_blocked", True),
            "publication-plan-unresolved",
        ),
    ],
)
async def test_epoch_gap_interval_and_plan_blockers_roll_back(
    configure: Any,
    code: str,
) -> None:
    app = _AppPool()
    configure(app)

    with pytest.raises(DaySealingBlocked) as raised:
        await _sealer(app).seal_day(DAY, 7)

    assert raised.value.code == code
    assert app.day_states == {}


@pytest.mark.asyncio
async def test_retired_epoch_only_requires_materialization_through_retirement() -> None:
    app = _AppPool()
    retirement = DAY_START + timedelta(hours=8)
    app.epochs = [
        _epoch(
            retired_at=retirement,
            complete_through=retirement,
        )
    ]
    app.snapshots = [_snapshot(received_at=retirement)]

    result = await _sealer(app).seal_day(DAY, 7)

    assert result.disposition is DaySealDisposition.SEALED
    assert result.coverage_status == "complete"


@pytest.mark.asyncio
async def test_waived_ranges_are_clipped_merged_and_partial() -> None:
    app = _AppPool()
    app.gaps = [
        _gap(
            start=DAY_START - timedelta(hours=1),
            end=DAY_START + timedelta(hours=2),
            resolution="waived",
            gap_id=UUID("30000000-0000-0000-0000-000000000001"),
        ),
        _gap(
            start=DAY_START + timedelta(hours=2),
            end=DAY_START + timedelta(hours=4),
            resolution="waived",
            gap_id=UUID("30000000-0000-0000-0000-000000000002"),
        ),
        _gap(
            start=DAY_START + timedelta(hours=8),
            end=DAY_START + timedelta(hours=9),
            resolution="backfilled",
            gap_id=UUID("30000000-0000-0000-0000-000000000003"),
        ),
    ]

    result = await _sealer(app).seal_day(DAY, 7)

    assert result.coverage_status == "partial"
    assert result.unknown_ranges == ((DAY_START, DAY_START + timedelta(hours=4)),)
    assert app.day_states[DAY]["unknown_ranges"] == [
        {
            "start": "2026-08-02T00:00:00.000000Z",
            "end": "2026-08-02T04:00:00.000000Z",
        }
    ]


@pytest.mark.asyncio
async def test_waiver_can_cover_epoch_watermark_shortfall() -> None:
    app = _AppPool()
    gap_start = DAY_START + timedelta(hours=1)
    app.epochs[0]["complete_through"] = gap_start
    app.snapshots = [_snapshot(received_at=gap_start)]
    app.gaps = [
        _gap(
            start=gap_start,
            end=DAY_END,
            resolution="waived",
        )
    ]

    result = await _sealer(app).seal_day(DAY, 7)

    assert result.coverage_status == "partial"
    assert result.unknown_ranges == ((gap_start, DAY_END),)


@pytest.mark.asyncio
async def test_invalid_snapshot_and_watch_inside_waived_tail_are_beyond_known_end() -> (
    None
):
    app = _AppPool()
    known_end = DAY_START + timedelta(hours=1)
    app.epochs[0]["complete_through"] = known_end
    app.snapshots = [
        _snapshot(received_at=known_end),
        _snapshot(
            received_at=known_end + timedelta(minutes=1),
            item_errors=[{"code": "inside-waived-tail"}],
        ),
    ]
    app.gaps = [
        _gap(start=known_end, end=DAY_END, resolution="waived"),
    ]
    app.watch_events = [
        _watch_event(received_at=known_end + timedelta(minutes=1)),
    ]

    result = await _sealer(app).seal_day(DAY, 7)

    assert result.coverage_status == "partial"
    assert result.unknown_ranges == ((known_end, DAY_END),)


@pytest.mark.asyncio
async def test_backfill_can_cover_epoch_watermark_shortfall_as_complete() -> None:
    app = _AppPool()
    gap_start = DAY_START + timedelta(hours=1)
    app.epochs[0]["complete_through"] = gap_start
    app.snapshots = [_snapshot(received_at=gap_start)]
    app.gaps = [
        _gap(
            start=gap_start,
            end=DAY_END,
            resolution="backfilled",
        )
    ]

    result = await _sealer(app).seal_day(DAY, 7)

    assert result.coverage_status == "complete"
    assert result.unknown_ranges == ()


@pytest.mark.asyncio
async def test_required_scope_must_match_enabled_resource_mapping() -> None:
    app = _AppPool()
    app.epochs[0]["api_resource"] = "core/v1/persistentvolumeclaims"

    with pytest.raises(DaySealingBlocked) as raised:
        await _sealer(app).seal_day(DAY, 7)

    assert raised.value.code == "required-source-not-enabled"


@pytest.mark.asyncio
async def test_coverage_revision_is_deterministic_and_commits_proof_changes() -> None:
    first_app = _AppPool()
    second_app = _AppPool()
    changed_app = _AppPool()
    changed_app.epochs[0]["complete_through"] += timedelta(seconds=1)

    first = await _sealer(first_app).seal_day(DAY, 7)
    second = await _sealer(second_app).seal_day(DAY, 7)
    changed = await _sealer(changed_app).seal_day(DAY, 7)

    assert first.coverage_revision == second.coverage_revision
    assert changed.coverage_revision != first.coverage_revision


@pytest.mark.asyncio
async def test_finish_failure_rolls_back_open_to_sealing_transition() -> None:
    app = _AppPool()
    app.day_states[DAY] = {
        "day": DAY,
        "state": "open",
        "coverage_status": None,
        "coverage_revision": None,
        "unknown_ranges": [],
    }
    app.finish_failure = "raise"

    with pytest.raises(RuntimeError, match="injected finish failure"):
        await _sealer(app).seal_day(DAY, 7)

    assert app.day_states[DAY]["state"] == "open"
    assert app.day_states[DAY]["coverage_revision"] is None
    assert app.rollbacks == 1


@pytest.mark.asyncio
async def test_lost_finish_fence_rolls_back_new_day_row() -> None:
    app = _AppPool()
    app.finish_failure = "fence"

    with pytest.raises(DaySealingFenceError, match="changed before seal commit"):
        await _sealer(app).seal_day(DAY, 7)

    assert app.day_states == {}
    assert app.rollbacks == 1
