"""Strict audit adapter tests for the legacy workspace cutover."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

from orchestrator.services.infrastructure_metering.cutover import (
    FrozenLegacyWorkspaceEvent,
    LegacyWorkspaceFreezeRequest,
    LegacyWorkspaceLedgerConflict,
    LegacyWorkspaceLedgerError,
    legacy_workspace_payload_hash,
)
from orchestrator.services.infrastructure_metering.cutover_ledger import (
    LegacyWorkspaceUsageLedgerAdapter,
)
from orchestrator.services.usage_ledger import (
    StrictUsageConflict,
    StrictUsagePartitionMissing,
    StrictUsagePublishResult,
    UsageLedger,
    UsageRates,
)

UTC = timezone.utc
START = datetime(2026, 8, 5, 8, tzinfo=UTC)
OWNER_ID = UUID("10000000-0000-0000-0000-000000000001")
USER_ID = UUID("20000000-0000-0000-0000-000000000002")
PROJECT_ID = UUID("30000000-0000-0000-0000-000000000003")
ADOPTED_USER_ID = UUID("40000000-0000-0000-0000-000000000004")
ADOPTED_PROJECT_ID = UUID("50000000-0000-0000-0000-000000000005")


class _Transaction:
    def __init__(self, connection: _AuditConnection, options: dict[str, Any]):
        self.connection = connection
        self.options = options

    async def __aenter__(self):
        assert not self.connection.in_transaction
        self.connection.in_transaction = True
        self.connection.transaction_options.append(self.options)
        return self

    async def __aexit__(self, *_args: Any):
        self.connection.in_transaction = False
        return False


class _AuditConnection:
    def __init__(self, rows: list[dict[str, Any]]):
        self.rows = rows
        self.in_transaction = False
        self.transaction_options: list[dict[str, Any]] = []
        self.fetch_calls: list[tuple[str, tuple[Any, ...]]] = []

    def transaction(self, **options: Any):
        return _Transaction(self, options)

    async def fetch(self, sql: str, *args: Any):
        assert self.in_transaction
        assert "infra-cutover-ledger:freeze-existing" in sql
        self.fetch_calls.append((sql, args))
        return copy.deepcopy(self.rows)


class _Acquire:
    def __init__(self, connection: _AuditConnection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, *_args: Any):
        return False


class _AuditPool:
    def __init__(self, rows: list[dict[str, Any]] | None = None):
        self.connection = _AuditConnection(rows or [])

    def acquire(self):
        return _Acquire(self.connection)


def _request() -> LegacyWorkspaceFreezeRequest:
    return LegacyWorkspaceFreezeRequest(
        workspace_interval_id=41,
        owner_kind="job",
        owner_id=OWNER_ID,
        tier="sandbox",
        cpu_millicores=8000,
        memory_bytes=16 * 1024**3,
        started_at=START,
        ended_at=START + timedelta(hours=1),
        user_id=USER_ID,
        project_id=PROJECT_ID,
    )


def _existing_row(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "ts": datetime.fromisoformat(payload["ts"].replace("Z", "+00:00")),
        "user_id": None if payload["user_id"] is None else UUID(payload["user_id"]),
        "project_id": (
            None if payload["project_id"] is None else UUID(payload["project_id"])
        ),
        "ref_kind": payload["ref_kind"],
        "ref_id": UUID(payload["ref_id"]),
        "category": payload["category"],
        "resource": payload["resource"],
        "quantity": Decimal(payload["quantity"]),
        "unit": payload["unit"],
        "rate_usd": (
            None if payload["rate_usd"] is None else Decimal(payload["rate_usd"])
        ),
        "cost_usd": (
            None if payload["cost_usd"] is None else Decimal(payload["cost_usd"])
        ),
        "source": payload["source"],
        "source_id": payload["source_id"],
        # The production write pool has asyncpg's default text JSONB codec.
        "details": json.dumps(payload["details"]),
    }


def _priced_payloads(
    request: LegacyWorkspaceFreezeRequest,
) -> tuple[dict[str, Any], dict[str, Any]]:
    cpu, memory = (dict(payload) for payload in request.draft_payloads())
    cpu.update(rate_usd="0.1", cost_usd="0.8")
    memory.update(rate_usd="0.02", cost_usd="0.32")
    return cpu, memory


def _dependencies(
    rows: list[dict[str, Any]] | None = None,
    *,
    rate_side_effect: Any = None,
):
    audit = _AuditPool(rows)
    ledger = MagicMock(spec=UsageLedger)
    ledger.publish_expected_events = AsyncMock()
    ledger.record_events = AsyncMock()
    rates = MagicMock(spec=UsageRates)
    rates.resolve = AsyncMock(side_effect=rate_side_effect)
    adapter = LegacyWorkspaceUsageLedgerAdapter(
        audit,
        ledger,
        rates,  # type: ignore[arg-type]
    )
    return adapter, audit, ledger, rates


@pytest.mark.asyncio
async def test_freeze_adopts_exact_existing_pair_and_replays_identically() -> None:
    request = _request()
    expected = _priced_payloads(request)
    adapter, audit, _ledger, rates = _dependencies(
        [_existing_row(payload) for payload in expected]
    )

    first = await adapter.freeze_legacy_workspace_events(request)
    replay = await adapter.freeze_legacy_workspace_events(request)

    assert [event.payload for event in first] == list(expected)
    assert [event.row_hash for event in replay] == [event.row_hash for event in first]
    assert audit.connection.transaction_options == [
        {"isolation": "repeatable_read", "readonly": True},
        {"isolation": "repeatable_read", "readonly": True},
    ]
    assert all(
        not options.get("deferrable")
        for options in audit.connection.transaction_options
    )
    assert len(audit.connection.fetch_calls) == 2
    assert audit.connection.fetch_calls[0][1] == (
        "orchestrator",
        request.source_id,
        request.ended_at,
    )
    rates.resolve.assert_not_awaited()


@pytest.mark.asyncio
async def test_partial_existing_pair_preserves_its_attribution() -> None:
    request = _request()
    cpu, _memory = _priced_payloads(request)
    cpu["user_id"] = str(ADOPTED_USER_ID)
    cpu["project_id"] = str(ADOPTED_PROJECT_ID)

    async def resolve(_category: str, _resource: str, unit: str, _ts: datetime):
        assert unit == "gib-hour"
        return Decimal("0.02")

    adapter, _audit, _ledger, rates = _dependencies(
        [_existing_row(cpu)], rate_side_effect=resolve
    )

    frozen = await adapter.freeze_legacy_workspace_events(request)
    by_unit = {event.payload["unit"]: event.payload for event in frozen}

    assert by_unit["vcpu-hour"] == cpu
    assert by_unit["gib-hour"]["user_id"] == str(ADOPTED_USER_ID)
    assert by_unit["gib-hour"]["project_id"] == str(ADOPTED_PROJECT_ID)
    assert by_unit["gib-hour"]["rate_usd"] == "0.02"
    assert by_unit["gib-hour"]["cost_usd"] == "0.32"
    rates.resolve.assert_awaited_once()


@pytest.mark.asyncio
async def test_missing_pair_freezes_resolved_rate_cost_and_free_zero() -> None:
    request = _request()

    async def resolve(_category: str, _resource: str, unit: str, _ts: datetime):
        return Decimal("0.125") if unit == "vcpu-hour" else Decimal("0")

    adapter, _audit, _ledger, rates = _dependencies(rate_side_effect=resolve)

    frozen = await adapter.freeze_legacy_workspace_events(request)
    by_unit = {event.payload["unit"]: event.payload for event in frozen}

    assert by_unit["vcpu-hour"]["rate_usd"] == "0.125"
    assert by_unit["vcpu-hour"]["cost_usd"] == "1"
    assert by_unit["gib-hour"]["rate_usd"] == "0"
    assert by_unit["gib-hour"]["cost_usd"] == "0"
    assert rates.resolve.await_count == 2


@pytest.mark.asyncio
async def test_missing_pair_freezes_unpriced_rows_explicitly() -> None:
    adapter, _audit, _ledger, rates = _dependencies(
        rate_side_effect=lambda *_args: None
    )

    frozen = await adapter.freeze_legacy_workspace_events(_request())

    assert all(event.payload["rate_usd"] is None for event in frozen)
    assert all(event.payload["cost_usd"] is None for event in frozen)
    assert rates.resolve.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["quantity", "pricing"])
async def test_freeze_rejects_incompatible_or_invalid_existing_row(
    invalid: str,
) -> None:
    request = _request()
    cpu, _memory = _priced_payloads(request)
    if invalid == "quantity":
        cpu["quantity"] = "9"
        cpu["cost_usd"] = "0.9"
    else:
        cpu["cost_usd"] = None
    adapter, _audit, _ledger, rates = _dependencies([_existing_row(cpu)])

    with pytest.raises(LegacyWorkspaceLedgerConflict):
        await adapter.freeze_legacy_workspace_events(request)

    rates.resolve.assert_not_awaited()


@pytest.mark.asyncio
async def test_freeze_rejects_ambiguous_existing_rows() -> None:
    request = _request()
    cpu, memory = _priced_payloads(request)
    memory["user_id"] = str(ADOPTED_USER_ID)
    adapter, _audit, _ledger, _rates = _dependencies(
        [_existing_row(cpu), _existing_row(memory)]
    )

    with pytest.raises(LegacyWorkspaceLedgerConflict, match="ambiguous"):
        await adapter.freeze_legacy_workspace_events(request)


def _unpriced_events() -> tuple[FrozenLegacyWorkspaceEvent, FrozenLegacyWorkspaceEvent]:
    return tuple(
        FrozenLegacyWorkspaceEvent(
            payload=payload,
            row_hash=legacy_workspace_payload_hash(payload),
        )
        for payload in _request().draft_payloads()
    )  # type: ignore[return-value]


@pytest.mark.asyncio
async def test_publish_maps_strict_insert_and_exact_replay_without_fallback() -> None:
    adapter, _audit, ledger, _rates = _dependencies()
    ledger.publish_expected_events.side_effect = [
        StrictUsagePublishResult(expected=2, inserted=2, verified=2),
        StrictUsagePublishResult(expected=2, inserted=0, verified=2),
    ]
    events = _unpriced_events()

    first = await adapter.publish_frozen_legacy_workspace_events(events)
    replay = await adapter.publish_frozen_legacy_workspace_events(events)

    assert (first.expected, first.inserted, first.verified) == (2, 2, 2)
    assert (replay.expected, replay.inserted, replay.verified) == (2, 0, 2)
    assert ledger.publish_expected_events.await_count == 2
    expectations = ledger.publish_expected_events.await_args_list[0].args[0]
    assert {expectation.unit for expectation in expectations} == {
        "vcpu-hour",
        "gib-hour",
    }
    assert all(len(expectation.expected_fields) == 14 for expectation in expectations)
    ledger.record_events.assert_not_awaited()


@pytest.mark.asyncio
async def test_publish_translates_strict_conflict_and_operational_error() -> None:
    adapter, _audit, ledger, _rates = _dependencies()
    ledger.publish_expected_events.side_effect = StrictUsageConflict("mismatch")

    with pytest.raises(LegacyWorkspaceLedgerConflict):
        await adapter.publish_frozen_legacy_workspace_events(_unpriced_events())

    ledger.publish_expected_events.side_effect = StrictUsagePartitionMissing(
        ["usage_events_p2026_08"]
    )
    with pytest.raises(LegacyWorkspaceLedgerError) as raised:
        await adapter.publish_frozen_legacy_workspace_events(_unpriced_events())
    assert not isinstance(raised.value, LegacyWorkspaceLedgerConflict)
    ledger.record_events.assert_not_awaited()


@pytest.mark.asyncio
async def test_publish_rejects_payload_mutated_after_it_was_frozen() -> None:
    adapter, _audit, ledger, _rates = _dependencies()
    events = _unpriced_events()
    events[0].payload["quantity"] = "999"  # type: ignore[index]

    with pytest.raises(LegacyWorkspaceLedgerError, match="strict contract"):
        await adapter.publish_frozen_legacy_workspace_events(events)

    ledger.publish_expected_events.assert_not_awaited()
    ledger.record_events.assert_not_awaited()
