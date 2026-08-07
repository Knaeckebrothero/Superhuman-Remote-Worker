"""Unit tests for the generation-fenced infrastructure UTC day sealer."""

from __future__ import annotations

import copy
import json
from datetime import date, datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest

from orchestrator.services.infrastructure_metering.materializer import (
    StoragePublicationAuthority,
    StoragePublicationPolicy,
)
from orchestrator.services.infrastructure_metering.sealer import (
    _INTERVAL_BLOCKER_SQL,
    _ITEM_BLOCKER_SQL,
    _PLAN_BLOCKER_SQL,
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


def _storage_policy(
    *authorities: tuple[str, str, str],
) -> StoragePublicationPolicy:
    return StoragePublicationPolicy(
        authorities=tuple(
            StoragePublicationAuthority(
                measurement_basis=basis,
                collector_id=collector_id,
                source_cluster=source_cluster,
            )
            for basis, collector_id, source_cluster in authorities
        )
    )


def _epoch(**overrides: Any) -> dict[str, Any]:
    row = {
        "id": EPOCH_ID,
        "scope_id": SCOPE_ID,
        "api_resource": "core/v1/pods",
        "collector_id": "kubernetes-pods",
        "source_cluster": "main-dev",
        "namespace": "srw",
        "required_from": CUTOVER,
        "reliable_from": CUTOVER - timedelta(hours=1),
        "continuous_since": CUTOVER - timedelta(hours=1),
        "complete_through": DAY_END + timedelta(hours=1),
        "retired_at": None,
    }
    row.update(overrides)
    return row


def _storage_requirement(
    *,
    measurement_basis: str = "claim-requested",
    collector_id: str = "kubernetes-pods",
    source_cluster: str = "main-dev",
    inventory_scope_id: UUID = SCOPE_ID,
    requirement_role: str = "quantity",
    effective_from: datetime = CUTOVER,
) -> dict[str, Any]:
    return {
        "measurement_basis": measurement_basis,
        "collector_id": collector_id,
        "source_cluster": source_cluster,
        "inventory_scope_id": inventory_scope_id,
        "requirement_role": requirement_role,
        "effective_from": effective_from,
    }


def _gap(
    *,
    start: datetime,
    end: datetime | None,
    resolution: str,
    gap_id: UUID | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    return {
        "id": gap_id or uuid4(),
        "scope_epoch_id": EPOCH_ID,
        "gap_start": start,
        "gap_end": end,
        "resolution": resolution,
        "reason": reason,
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
        if "infra-seal:compute-epoch-set-lock" in sql:
            return copy.deepcopy(self.pool.compute_requirements)
        if "infra-seal:compute-requirements" in sql:
            return copy.deepcopy(self.pool.compute_requirements)
        if "infra-seal:compute-activations" in sql:
            return copy.deepcopy(self.pool.compute_activations)
        if "infra-seal:epochs" in sql:
            return copy.deepcopy(self.pool.epochs)
        if "infra-seal:storage-requirements" in sql:
            return copy.deepcopy(self.pool.storage_requirements)
        if "infra-seal:gaps" in sql or "infra-seal:storage-gaps" in sql:
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
            self.pool.interval_query_args = args
            return {"id": uuid4()} if self.pool.interval_blocked else None
        if "infra-seal:plan-blocker" in sql:
            self.pool.plan_query_args = args
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
        self.compute_activations: list[dict[str, Any]] = []
        self.compute_requirements: list[dict[str, Any]] = []
        self.storage_requirements: list[dict[str, Any]] = []
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
        self.interval_query_args: tuple[Any, ...] | None = None
        self.plan_query_args: tuple[Any, ...] | None = None

    def acquire(self) -> _Acquire:
        return _Acquire(self)


def _sealer(
    pool: _AppPool,
    *,
    enabled: bool = True,
    enabled_resources: tuple[str, ...] = ("workspace_pod",),
    storage_publication_policy: StoragePublicationPolicy | None = None,
) -> InfrastructureUsageDaySealer:
    return InfrastructureUsageDaySealer(
        pool,  # type: ignore[arg-type]
        sealing_enabled=enabled,
        enabled_resources=enabled_resources,
        storage_publication_policy=storage_publication_policy,
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


def test_storage_blocker_sql_is_exact_and_conservatively_rejects_wrong_sources():
    for sql in (_INTERVAL_BLOCKER_SQL, _PLAN_BLOCKER_SQL):
        compact = " ".join(sql.split())
        assert "unnest($5::text[], $6::text[], $7::text[])" in compact
        assert "NOT EXISTS" in compact
        assert "storage_policy.collector_id = source_scope.collector_id" in compact
        assert "storage_policy.source_cluster = source_scope.source_cluster" in compact
        assert "source_scope.source_cluster = interval.source_cluster" in compact


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


def test_compute_seal_rejects_successor_inheritance_after_exact_epoch_retirement() -> (
    None
):
    retirement = DAY_START + timedelta(hours=12)
    successor_id = UUID("10000000-0000-0000-0000-000000000002")
    sealer = InfrastructureUsageDaySealer(  # type: ignore[arg-type]
        object(),
        enabled_resources=("workspace_pod", "agent_pod"),
    )
    activations = (
        {
            "activation_key": "agent_pod",
            "state": "active",
            "activated_at": DAY_START,
            "database_time": OBSERVED_AT,
        },
    )
    requirements = (
        {
            "activation_key": "agent_pod",
            "inventory_scope_id": SCOPE_ID,
            "inventory_scope_epoch_id": EPOCH_ID,
            "authority_sequence": 1,
            "authority_effective_from": DAY_START,
            "reliable_from": DAY_START,
            "continuous_since": DAY_START,
            "complete_through": retirement,
            "retired_at": retirement,
            "api_resource": "core/v1/pods",
        },
    )
    epochs = (
        _epoch(required_from=DAY_START, retired_at=retirement),
        _epoch(
            id=successor_id,
            required_from=retirement,
            reliable_from=retirement,
            continuous_since=retirement,
        ),
    )

    with pytest.raises(
        DaySealingBlocked,
        match="required-compute-exact-epoch-retired",
    ):
        sealer._validate_compute_requirements(
            activations,
            requirements,
            epochs=epochs,
            seal_start=DAY_START,
            day_end=DAY_END,
        )


def test_compute_seal_accepts_audited_successor_and_preserves_gap() -> None:
    retirement = DAY_START + timedelta(hours=10)
    promoted_at = retirement + timedelta(minutes=4)
    successor_id = UUID("10000000-0000-0000-0000-000000000003")
    sealer = InfrastructureUsageDaySealer(  # type: ignore[arg-type]
        object(),
        enabled_resources=("workspace_pod", "agent_pod"),
    )
    activations = (
        {
            "activation_key": "agent_pod",
            "state": "active",
            "activated_at": DAY_START,
            "database_time": OBSERVED_AT,
        },
    )
    requirements = (
        {
            "activation_key": "agent_pod",
            "inventory_scope_id": SCOPE_ID,
            "inventory_scope_epoch_id": EPOCH_ID,
            "authority_sequence": 1,
            "authority_effective_from": DAY_START,
            "reliable_from": DAY_START,
            "continuous_since": DAY_START,
            "complete_through": retirement,
            "retired_at": retirement,
            "api_resource": "core/v1/pods",
        },
        {
            "activation_key": "agent_pod",
            "inventory_scope_id": SCOPE_ID,
            "inventory_scope_epoch_id": successor_id,
            "authority_sequence": 2,
            "authority_effective_from": promoted_at,
            "reliable_from": promoted_at,
            "continuous_since": promoted_at,
            "complete_through": DAY_END,
            "retired_at": None,
            "api_resource": "core/v1/pods",
        },
    )

    manifest, missing = sealer._validate_compute_requirements(
        activations,
        requirements,
        epochs=(),
        seal_start=DAY_START,
        day_end=DAY_END,
    )

    assert [entry["authority_sequence"] for entry in manifest] == [1, 2]
    assert missing == {("agent_pod", str(successor_id)): [(retirement, promoted_at)]}


def test_compute_authority_gap_evidence_is_class_scoped() -> None:
    start = DAY_START + timedelta(hours=4)
    end = start + timedelta(minutes=3)
    gap = _gap(
        start=start,
        end=end,
        resolution="waived",
        reason="compute-authority-awaiting-confirmation:agent_pod",
    )

    ignored = InfrastructureUsageDaySealer._validate_gaps(
        (gap,),
        seal_start=DAY_START,
        day_end=DAY_END,
        enabled_compute_keys=frozenset({"ide_workspace_pod"}),
    )
    assert ignored == ([], (), {}, {})

    manifest, unknown, generic, compute = InfrastructureUsageDaySealer._validate_gaps(
        (gap,),
        seal_start=DAY_START,
        day_end=DAY_END,
        enabled_compute_keys=frozenset({"agent_pod"}),
    )
    assert manifest[0]["reason"] == gap["reason"]
    assert unknown == ((start, end),)
    assert generic == {}
    assert compute == {("agent_pod", str(EPOCH_ID)): [(start, end)]}


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
async def test_generic_epoch_gap_can_cover_same_compute_authority_shortfall() -> None:
    app = _AppPool()
    gap_start = DAY_START + timedelta(hours=1)
    app.epochs[0]["complete_through"] = gap_start
    app.snapshots = [_snapshot(received_at=gap_start)]
    app.compute_activations = [
        {
            "activation_key": "agent_pod",
            "state": "active",
            "activated_at": DAY_START,
            "database_time": OBSERVED_AT,
        }
    ]
    app.compute_requirements = [
        {
            "activation_key": "agent_pod",
            "inventory_scope_id": SCOPE_ID,
            "inventory_scope_epoch_id": EPOCH_ID,
            "authority_sequence": 1,
            "authority_effective_from": DAY_START,
            "reliable_from": CUTOVER - timedelta(hours=1),
            "continuous_since": CUTOVER - timedelta(hours=1),
            "complete_through": gap_start,
            "retired_at": None,
            "api_resource": "core/v1/pods",
        }
    ]
    app.gaps = [
        _gap(start=gap_start, end=DAY_END, resolution="waived"),
    ]

    result = await _sealer(
        app,
        enabled_resources=("workspace_pod", "agent_pod"),
    ).seal_day(DAY, 7)

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
@pytest.mark.parametrize(
    "identity_override",
    (
        {"collector_id": "kubevirt-storage"},
        {"source_cluster": "vm-cluster"},
    ),
)
async def test_identical_storage_api_resources_do_not_share_authority(
    identity_override: dict[str, str],
) -> None:
    app = _AppPool()
    other_epoch = _epoch(
        id=UUID("10000000-0000-0000-0000-000000000002"),
        scope_id=UUID("20000000-0000-0000-0000-000000000002"),
        api_resource="core/v1/persistentvolumeclaims",
        **identity_override,
    )
    app.epochs = [
        _epoch(api_resource="core/v1/persistentvolumeclaims"),
        other_epoch,
    ]
    app.storage_requirements = [_storage_requirement()]
    policy = _storage_policy(("claim-requested", "kubernetes-pods", "main-dev"))

    with pytest.raises(DaySealingBlocked) as raised:
        await _sealer(
            app,
            enabled_resources=("workspace_pvc",),
            storage_publication_policy=policy,
        ).seal_day(DAY, 7)

    assert raised.value.code == "required-storage-authority-not-enabled"
    assert app.day_states == {}


@pytest.mark.asyncio
async def test_configured_storage_authority_requires_an_overlapping_epoch() -> None:
    app = _AppPool()
    policy = _storage_policy(("claim-requested", "kubevirt-storage", "vm-cluster"))
    app.storage_requirements = [
        _storage_requirement(
            collector_id="kubevirt-storage",
            source_cluster="vm-cluster",
            inventory_scope_id=UUID("20000000-0000-0000-0000-000000000010"),
        )
    ]

    with pytest.raises(DaySealingBlocked) as raised:
        await _sealer(
            app,
            enabled_resources=("workspace_pod", "workspace_pvc"),
            storage_publication_policy=policy,
        ).seal_day(DAY, 7)

    assert raised.value.code == "required-storage-source-missing"
    assert app.day_states == {}


@pytest.mark.asyncio
async def test_mapped_volume_resource_enables_pv_scope_and_storage_gap_path() -> None:
    app = _AppPool()
    pvc_scope_id = UUID("20000000-0000-0000-0000-000000000011")
    pvc_epoch_id = UUID("10000000-0000-0000-0000-000000000011")
    app.epochs = [
        _epoch(api_resource="core/v1/persistentvolumes", namespace=None),
        _epoch(
            id=pvc_epoch_id,
            scope_id=pvc_scope_id,
            api_resource="core/v1/persistentvolumeclaims",
        ),
    ]
    app.storage_requirements = [
        _storage_requirement(measurement_basis="volume-provisioned"),
        _storage_requirement(
            measurement_basis="volume-provisioned",
            inventory_scope_id=pvc_scope_id,
            requirement_role="attribution",
        ),
    ]
    pvc_snapshot = _snapshot(received_at=DAY_END)
    pvc_snapshot["scope_epoch_id"] = pvc_epoch_id
    app.snapshots.append(pvc_snapshot)
    policy = _storage_policy(("volume-provisioned", "kubernetes-pods", "main-dev"))
    sealer = InfrastructureUsageDaySealer(
        app,  # type: ignore[arg-type]
        sealing_enabled=True,
        enabled_resources=("workspace_pod", "block_volume_local_path"),
        storage_publication_policy=policy,
    )
    manifest, _missing = sealer._validate_epochs(
        app.epochs,
        storage_requirements=app.storage_requirements,
        seal_start=DAY_START,
        day_end=DAY_END,
    )

    result = await sealer.seal_day(DAY, 7)

    assert result.coverage_status == "complete"
    pvc_manifest = next(
        row
        for row in manifest
        if row["api_resource"] == "core/v1/persistentvolumeclaims"
    )
    assert pvc_manifest["measurement_basis"] == "volume-provisioned"
    assert pvc_manifest["storage_requirements"] == [
        {
            "measurement_basis": "volume-provisioned",
            "requirement_role": "attribution",
            "effective_from": CUTOVER,
        }
    ]
    assert app.interval_query_args is not None
    assert app.plan_query_args is not None
    expected_policy_columns = (
        ["volume-provisioned"],
        ["kubernetes-pods"],
        ["main-dev"],
    )
    assert app.interval_query_args[4:] == expected_policy_columns
    assert app.plan_query_args[4:] == expected_policy_columns


@pytest.mark.asyncio
async def test_pv_seal_fails_closed_without_required_pvc_attribution_epoch() -> None:
    app = _AppPool()
    missing_pvc_scope_id = UUID("20000000-0000-0000-0000-000000000012")
    app.epochs[0].update(
        {"api_resource": "core/v1/persistentvolumes", "namespace": None}
    )
    app.storage_requirements = [
        _storage_requirement(measurement_basis="volume-provisioned"),
        _storage_requirement(
            measurement_basis="volume-provisioned",
            inventory_scope_id=missing_pvc_scope_id,
            requirement_role="attribution",
        ),
    ]
    policy = _storage_policy(("volume-provisioned", "kubernetes-pods", "main-dev"))

    with pytest.raises(DaySealingBlocked) as raised:
        await _sealer(
            app,
            enabled_resources=("block_volume_local_path",),
            storage_publication_policy=policy,
        ).seal_day(DAY, 7)

    assert raised.value.code == "required-storage-source-missing"
    assert app.day_states == {}


@pytest.mark.asyncio
async def test_storage_epoch_identity_is_in_deterministic_manifest() -> None:
    policy = _storage_policy(("claim-requested", "kubernetes-pods", "main-dev"))
    first_app = _AppPool()
    second_app = _AppPool()
    first_app.epochs[0].update(
        {
            "api_resource": "core/v1/persistentvolumeclaims",
            "namespace": "srw-a",
        }
    )
    second_app.epochs[0].update(
        {
            "api_resource": "core/v1/persistentvolumeclaims",
            "namespace": "srw-b",
        }
    )
    first_app.storage_requirements = [_storage_requirement()]
    second_app.storage_requirements = [_storage_requirement()]
    first_sealer = _sealer(
        first_app,
        enabled_resources=("workspace_pvc",),
        storage_publication_policy=policy,
    )
    manifest, _missing = first_sealer._validate_epochs(
        first_app.epochs,
        storage_requirements=first_app.storage_requirements,
        seal_start=DAY_START,
        day_end=DAY_END,
    )

    first = await first_sealer.seal_day(DAY, 7)
    second = await _sealer(
        second_app,
        enabled_resources=("workspace_pvc",),
        storage_publication_policy=policy,
    ).seal_day(DAY, 7)

    assert {
        field: manifest[0][field]
        for field in (
            "measurement_basis",
            "collector_id",
            "source_cluster",
            "namespace",
        )
    } == {
        "measurement_basis": "claim-requested",
        "collector_id": "kubernetes-pods",
        "source_cluster": "main-dev",
        "namespace": "srw-a",
    }
    assert first.coverage_revision != second.coverage_revision


@pytest.mark.asyncio
async def test_storage_epoch_replacement_preserves_the_exact_day_boundary() -> None:
    boundary = DAY_START + timedelta(hours=8)
    replacement_epoch_id = UUID("10000000-0000-0000-0000-000000000003")
    policy = _storage_policy(("claim-requested", "kubernetes-pods", "main-dev"))
    app = _AppPool()
    app.storage_requirements = [_storage_requirement()]
    app.epochs = [
        _epoch(
            api_resource="core/v1/persistentvolumeclaims",
            retired_at=boundary,
            complete_through=boundary,
        ),
        _epoch(
            id=replacement_epoch_id,
            api_resource="core/v1/persistentvolumeclaims",
            required_from=boundary,
            complete_through=DAY_END,
        ),
    ]
    replacement_snapshot = _snapshot(received_at=DAY_END)
    replacement_snapshot["scope_epoch_id"] = replacement_epoch_id
    app.snapshots = [
        _snapshot(received_at=boundary),
        replacement_snapshot,
    ]

    result = await _sealer(
        app,
        enabled_resources=("workspace_pvc",),
        storage_publication_policy=policy,
    ).seal_day(DAY, 7)

    assert result.coverage_status == "complete"
    assert result.required_scopes == 1


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
