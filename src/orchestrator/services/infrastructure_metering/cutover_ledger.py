"""Strict audit-ledger adapter for the legacy workspace cutover.

The cutover coordinator owns durable intent in the app database, while the
legacy usage rows live in the audit database.  This adapter is the deliberately
narrow bridge between them: it freezes both legacy dimensions from one audit
snapshot, then delegates the eventual all-or-nothing insert/verification to the
strict :class:`UsageLedger` boundary.  It never calls the legacy best-effort
``record_events`` path.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

import asyncpg

from orchestrator.services.usage_ledger import (
    StrictUsageConflict,
    StrictUsageExpectation,
    StrictUsageLedgerError,
    UsageLedger,
    UsageRates,
)
from orchestrator.services.infrastructure_metering.cutover import (
    CutoverContractError,
    FrozenLegacyWorkspaceEvent,
    LegacyWorkspaceFreezeRequest,
    LegacyWorkspaceLedgerConflict,
    LegacyWorkspaceLedgerError,
    LegacyWorkspacePublishResult,
    legacy_workspace_payload_hash,
)

_UTC = timezone.utc
_UNITS = ("vcpu-hour", "gib-hour")
_ATTRIBUTION_FIELDS = ("user_id", "project_id", "ref_kind", "ref_id")

_EXISTING_LEGACY_ROWS_SQL = """
/* infra-cutover-ledger:freeze-existing */
WITH expected(unit) AS (
    VALUES ('vcpu-hour'::text), ('gib-hour'::text)
)
SELECT
    actual.ts, actual.user_id, actual.project_id, actual.ref_kind,
    actual.ref_id, actual.category, actual.resource, actual.quantity,
    actual.unit, actual.rate_usd, actual.cost_usd, actual.source,
    actual.source_id, actual.details
FROM expected
JOIN usage_events AS actual
  ON actual.source = $1
 AND actual.source_id = $2
 AND actual.unit = expected.unit
 AND actual.ts = $3
ORDER BY actual.unit
"""


def _timestamp(value: Any, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(_UTC)


def _timestamp_text(value: Any, field_name: str) -> str:
    return (
        _timestamp(value, field_name)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _uuid_text(value: Any, field_name: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    try:
        return str(value if isinstance(value, UUID) else UUID(str(value)))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a UUID") from exc


def _decimal(value: Any, field_name: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field_name} must be a decimal")
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a decimal") from exc
    if not number.is_finite():
        raise ValueError(f"{field_name} must be finite")
    return Decimal(0) if number.is_zero() else number


def _decimal_text(value: Any, field_name: str) -> str:
    """Canonicalize legacy NUMERIC without imposing the v2 38,18 quantum."""

    number = _decimal(value, field_name)
    rendered = format(number, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be non-empty text")
    return value


def _details(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("details must be valid JSON") from exc
    if not isinstance(value, Mapping):
        raise ValueError("details must be a JSON object")
    # Round-trip once so custom mapping/scalar implementations cannot leak into
    # the frozen hash and so non-JSON values fail before durable intent is made.
    try:
        return json.loads(
            json.dumps(
                dict(value),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("details contains a non-JSON value") from exc


def _payload_from_existing(row: Mapping[str, Any]) -> dict[str, Any]:
    payload = {
        "ts": _timestamp_text(row["ts"], "ts"),
        "user_id": _uuid_text(row["user_id"], "user_id", nullable=True),
        "project_id": _uuid_text(row["project_id"], "project_id", nullable=True),
        "ref_kind": _required_text(row["ref_kind"], "ref_kind"),
        "ref_id": _uuid_text(row["ref_id"], "ref_id"),
        "category": _required_text(row["category"], "category"),
        "resource": _required_text(row["resource"], "resource"),
        "quantity": _decimal_text(row["quantity"], "quantity"),
        "unit": _required_text(row["unit"], "unit"),
        "rate_usd": (
            None
            if row["rate_usd"] is None
            else _decimal_text(row["rate_usd"], "rate_usd")
        ),
        "cost_usd": (
            None
            if row["cost_usd"] is None
            else _decimal_text(row["cost_usd"], "cost_usd")
        ),
        "source": _required_text(row["source"], "source"),
        "source_id": _required_text(row["source_id"], "source_id"),
        "details": _details(row["details"]),
    }
    _validate_pricing(payload)
    return payload


def _validate_pricing(payload: Mapping[str, Any]) -> None:
    quantity = _decimal(payload["quantity"], "quantity")
    if quantity < 0:
        raise ValueError("quantity cannot be negative")
    raw_rate = payload["rate_usd"]
    raw_cost = payload["cost_usd"]
    if (raw_rate is None) != (raw_cost is None):
        raise ValueError("rate_usd and cost_usd must both be null or both be set")
    if raw_rate is None:
        return
    rate = _decimal(raw_rate, "rate_usd")
    cost = _decimal(raw_cost, "cost_usd")
    if rate < 0 or cost < 0:
        raise ValueError("rate_usd and cost_usd cannot be negative")
    if cost != quantity * rate:
        raise ValueError("cost_usd does not equal quantity times rate_usd")


def _frozen(payload: Mapping[str, Any]) -> FrozenLegacyWorkspaceEvent:
    frozen_payload = dict(payload)
    return FrozenLegacyWorkspaceEvent(
        payload=frozen_payload,
        row_hash=legacy_workspace_payload_hash(frozen_payload),
    )


class LegacyWorkspaceUsageLedgerAdapter:
    """Concrete strict ledger used by ``InfrastructureWorkspaceCutover``."""

    def __init__(
        self,
        audit_pool: asyncpg.Pool,
        usage_ledger: UsageLedger,
        usage_rates: UsageRates,
    ) -> None:
        self._audit = audit_pool
        self._ledger = usage_ledger
        self._rates = usage_rates

    async def freeze_legacy_workspace_events(
        self, request: LegacyWorkspaceFreezeRequest
    ) -> tuple[FrozenLegacyWorkspaceEvent, FrozenLegacyWorkspaceEvent]:
        """Adopt/price the immutable pair from one audit snapshot."""

        drafts = {str(payload["unit"]): payload for payload in request.draft_payloads()}
        try:
            async with self._audit.acquire() as conn:
                async with conn.transaction(isolation="repeatable_read", readonly=True):
                    rows = await conn.fetch(
                        _EXISTING_LEGACY_ROWS_SQL,
                        "orchestrator",
                        request.source_id,
                        request.ended_at.astimezone(_UTC),
                    )
        except Exception as exc:
            raise LegacyWorkspaceLedgerError(
                "legacy audit snapshot could not be read"
            ) from exc

        existing: dict[str, dict[str, Any]] = {}
        for raw_row in rows:
            try:
                payload = _payload_from_existing(raw_row)
                unit = payload["unit"]
                if unit not in drafts:
                    raise ValueError("existing row has an unsupported unit")
                expected_key = (
                    "orchestrator",
                    request.source_id,
                    unit,
                    drafts[unit]["ts"],
                )
                actual_key = (
                    payload["source"],
                    payload["source_id"],
                    unit,
                    payload["ts"],
                )
                if actual_key != expected_key:
                    raise ValueError("existing row does not match its full dedupe key")
                if unit in existing:
                    raise ValueError("multiple rows match one legacy dedupe key")
                event = _frozen(payload)
                event.validate_for(request)
            except (CutoverContractError, KeyError, TypeError, ValueError) as exc:
                raise LegacyWorkspaceLedgerConflict(
                    "existing legacy audit row is incompatible or invalid"
                ) from exc
            existing[unit] = payload

        existing_attribution = {
            tuple(payload[field] for field in _ATTRIBUTION_FIELDS)
            for payload in existing.values()
        }
        if len(existing_attribution) > 1:
            raise LegacyWorkspaceLedgerConflict(
                "existing legacy audit rows have ambiguous attribution"
            )
        attribution = (
            next(iter(existing_attribution))
            if existing_attribution
            else tuple(drafts[_UNITS[0]][field] for field in _ATTRIBUTION_FIELDS)
        )

        frozen_events: list[FrozenLegacyWorkspaceEvent] = []
        for unit in _UNITS:
            payload = existing.get(unit)
            if payload is None:
                payload = dict(drafts[unit])
                for field_name, value in zip(
                    _ATTRIBUTION_FIELDS, attribution, strict=True
                ):
                    payload[field_name] = value
                try:
                    rate = await self._rates.resolve(
                        str(payload["category"]),
                        str(payload["resource"]),
                        unit,
                        request.ended_at.astimezone(_UTC),
                    )
                    if rate is not None:
                        canonical_rate = _decimal(rate, "rate_usd")
                        if canonical_rate < 0:
                            raise ValueError("rate_usd cannot be negative")
                        quantity = _decimal(payload["quantity"], "quantity")
                        payload["rate_usd"] = _decimal_text(canonical_rate, "rate_usd")
                        payload["cost_usd"] = _decimal_text(
                            quantity * canonical_rate, "cost_usd"
                        )
                    _validate_pricing(payload)
                except Exception as exc:
                    raise LegacyWorkspaceLedgerError(
                        "legacy workspace rate could not be frozen"
                    ) from exc
            try:
                event = _frozen(payload)
                event.validate_for(request)
            except (CutoverContractError, KeyError, TypeError, ValueError) as exc:
                raise LegacyWorkspaceLedgerError(
                    "legacy workspace frozen event violates the cutover contract"
                ) from exc
            frozen_events.append(event)

        return frozen_events[0], frozen_events[1]

    async def publish_frozen_legacy_workspace_events(
        self, events: Sequence[FrozenLegacyWorkspaceEvent]
    ) -> LegacyWorkspacePublishResult:
        """Atomically insert and verify the exact legacy pair."""

        try:
            expectations = self._publish_expectations(events)
            result = await self._ledger.publish_expected_events(expectations)
        except StrictUsageConflict as exc:
            raise LegacyWorkspaceLedgerConflict(
                "legacy audit row conflicts with the frozen cutover plan"
            ) from exc
        except StrictUsageLedgerError as exc:
            raise LegacyWorkspaceLedgerError(
                "strict legacy audit publication failed"
            ) from exc
        except LegacyWorkspaceLedgerError:
            raise
        except Exception as exc:
            raise LegacyWorkspaceLedgerError(
                "strict legacy audit publication failed"
            ) from exc

        try:
            expected = int(result.expected)
            inserted = int(result.inserted)
            verified = int(result.verified)
        except (AttributeError, TypeError, ValueError) as exc:
            raise LegacyWorkspaceLedgerError(
                "strict legacy audit publication returned an invalid result"
            ) from exc
        if (
            expected != len(expectations)
            or verified != len(expectations)
            or inserted < 0
            or inserted > len(expectations)
        ):
            raise LegacyWorkspaceLedgerError(
                "strict legacy audit publication returned an incomplete result"
            )
        return LegacyWorkspacePublishResult(
            expected=expected,
            inserted=inserted,
            verified=verified,
        )

    @staticmethod
    def _publish_expectations(
        events: Sequence[FrozenLegacyWorkspaceEvent],
    ) -> tuple[StrictUsageExpectation, StrictUsageExpectation]:
        if len(events) != 2:
            raise LegacyWorkspaceLedgerError(
                "legacy publication requires exactly two frozen events"
            )
        by_unit: dict[str, FrozenLegacyWorkspaceEvent] = {}
        common: tuple[Any, ...] | None = None
        expectations: list[StrictUsageExpectation] = []
        for event in events:
            payload = dict(event.payload)
            unit = str(payload.get("unit"))
            if unit not in _UNITS or unit in by_unit:
                raise LegacyWorkspaceLedgerError(
                    "legacy publication requires one CPU and one RAM event"
                )
            try:
                if legacy_workspace_payload_hash(payload) != event.row_hash:
                    raise ValueError("legacy publication row hash changed")
                _validate_pricing(payload)
                timestamp = datetime.fromisoformat(
                    str(payload["ts"]).replace("Z", "+00:00")
                )
                timestamp = _timestamp(timestamp, "ts")
                if _timestamp_text(timestamp, "ts") != payload["ts"]:
                    raise ValueError("legacy publication timestamp is not canonical")
                event_common = (
                    payload["source"],
                    payload["source_id"],
                    payload["ts"],
                    *(payload[field] for field in _ATTRIBUTION_FIELDS),
                )
                if payload["source"] != "orchestrator":
                    raise ValueError("legacy publication source is invalid")
                if payload["category"] != "compute":
                    raise ValueError("legacy publication category is invalid")
                if payload["resource"] != "workspace_pod":
                    raise ValueError("legacy publication resource is invalid")
                if common is not None and event_common != common:
                    raise ValueError("legacy publication pair is inconsistent")
                common = event_common
                expectation = StrictUsageExpectation(
                    source=str(payload["source"]),
                    source_id=str(payload["source_id"]),
                    unit=unit,
                    ts=timestamp,
                    expected_fields=payload,
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise LegacyWorkspaceLedgerError(
                    "legacy publication event violates the strict contract"
                ) from exc
            by_unit[unit] = event
            expectations.append(expectation)
        return tuple(sorted(expectations, key=lambda item: _UNITS.index(item.unit)))  # type: ignore[return-value]


__all__ = ["LegacyWorkspaceUsageLedgerAdapter"]
