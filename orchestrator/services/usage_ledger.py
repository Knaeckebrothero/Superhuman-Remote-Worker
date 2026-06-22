"""Usage-metering ledger — the canonical cost/usage write+read path (Slice 4).

The append-only ``usage_events`` table (migrations/audit/0002) is the system of
record for metered cost: LLM tokens (materialized from the LiteLLM spend log,
Slice 4c) and workspace compute (emitted by the orchestrator at open/close,
Slice 4b). This module owns the **write** path (idempotent, rate-snapshotting)
and the raw aggregate **read** that ``/api/usage`` serves; the :class:`AuditStore`
reader stays focused on the agent trace.

Design: ``docs/features/observability_and_quotas.md`` ("The spine: one usage
ledger" + "The rate table"). Two pieces:

- :class:`UsageRates` — effective-dated $/unit resolver over the app-DB
  ``usage_rates`` table. Ships inert (empty table) → returns ``None`` (unpriced),
  so quantities are metered immediately and ``cost_usd`` fills in only once an
  admin seeds rates.
- :class:`UsageLedger` — bulk idempotent INSERT into ``usage_events`` (ON CONFLICT
  on the at-least-once dedupe key) with the rate snapshotted onto each row, plus
  the visibility-scoped aggregate read.

Non-load-bearing, like the rest of the audit tier: when the audit pool is absent
(``AUDIT_POSTGRES_*`` unset / connect failed) writes no-op and reads return empty
— metering disabled, product flow unaffected.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence

import asyncpg

logger = logging.getLogger(__name__)


def _dec(x: Any) -> Optional[Decimal]:
    """Coerce to Decimal for asyncpg's ``numeric`` codec (None stays None)."""
    return None if x is None else Decimal(str(x))


def _uuid(x: Any) -> Optional[uuid.UUID]:
    """Coerce to uuid.UUID (None stays None); raises on a malformed id."""
    if x is None:
        return None
    return x if isinstance(x, uuid.UUID) else uuid.UUID(str(x))


@dataclass
class UsageEvent:
    """One metered resource *dimension* (a vcpu-hour line, a prompt-token line).

    ``rate_usd`` / ``cost_usd`` are left None by emitters; :meth:`UsageLedger.
    record_events` snapshots the rate at write time. ``ts`` is the usage / interval
    -end time (emitter-supplied so the dedupe key is deterministic).
    """

    category: str  # 'llm' | 'compute' | 'query' | 'storage'
    resource: str  # '<model_id>' | 'workspace_pod' | ...
    quantity: Any  # int/float/Decimal in `unit`
    unit: str  # 'prompt-token' | 'vcpu-hour' | ...
    source: str  # 'litellm' | 'orchestrator' (idempotency namespace)
    source_id: str  # request_id / deterministic interval key
    ts: datetime
    user_id: Optional[str] = None
    project_id: Optional[str] = None
    ref_kind: Optional[str] = None  # 'job' | 'thread'
    ref_id: Optional[str] = None
    rate_usd: Any = None
    cost_usd: Any = None
    details: Dict[str, Any] = field(default_factory=dict)


class UsageRates:
    """Effective-dated rate resolver over the app-DB ``usage_rates`` table.

    The table is small admin config → cache all rows in memory with a short TTL
    and resolve the newest ``effective_from <= ts`` in Python (specific resource
    first, then the ``'*'`` category default). Returns ``None`` when unpriced (the
    v1 default — table ships empty), so the event's quantity is metered and its
    cost stays NULL.
    """

    def __init__(self, app_pool: Optional[asyncpg.Pool], *, ttl_s: float = 300.0):
        self._pool = app_pool
        self._ttl = ttl_s
        self._rows: List[Dict[str, Any]] = []
        self._loaded_at: float = -1.0

    async def _ensure_loaded(self) -> None:
        if self._pool is None:
            return
        now = time.monotonic()
        if self._loaded_at >= 0 and (now - self._loaded_at) < self._ttl:
            return
        try:
            rows = await self._pool.fetch(
                "SELECT category, resource, unit, rate_usd, effective_from "
                "FROM usage_rates"
            )
            self._rows = [dict(r) for r in rows]
            self._loaded_at = now
        except Exception:
            # Missing table / transient error → treat as unpriced, retry next call.
            logger.warning(
                "usage_rates load failed; treating as unpriced", exc_info=True
            )

    async def resolve(
        self, category: str, resource: str, unit: str, ts: datetime
    ) -> Optional[Decimal]:
        await self._ensure_loaded()
        if not self._rows:
            return None
        for want in (resource, "*"):
            cands = [
                r
                for r in self._rows
                if r["category"] == category
                and r["resource"] == want
                and r["unit"] == unit
                and r["effective_from"] <= ts
            ]
            if cands:
                return max(cands, key=lambda r: r["effective_from"])["rate_usd"]
        return None


# Parallel-unnest bulk insert. details rides as text[] and casts to jsonb in SQL
# (codec-independent — no dependency on the pool having a jsonb codec). ON CONFLICT
# on the dedupe index makes a re-polled spend log / re-emitted close a no-op.
_INSERT_SQL = """
INSERT INTO usage_events
    (ts, user_id, project_id, ref_kind, ref_id, category, resource,
     quantity, unit, rate_usd, cost_usd, source, source_id, details)
SELECT ts, user_id, project_id, ref_kind, ref_id, category, resource,
       quantity, unit, rate_usd, cost_usd, source, source_id, details::jsonb
FROM unnest(
    $1::timestamptz[], $2::uuid[], $3::uuid[], $4::text[], $5::uuid[],
    $6::text[], $7::text[], $8::numeric[], $9::text[], $10::numeric[],
    $11::numeric[], $12::text[], $13::text[], $14::text[]
) AS t(ts, user_id, project_id, ref_kind, ref_id, category, resource,
       quantity, unit, rate_usd, cost_usd, source, source_id, details)
ON CONFLICT (source, source_id, unit, ts) DO NOTHING
"""


class UsageLedger:
    """Write + raw-read surface for the ``usage_events`` ledger (auditdb)."""

    def __init__(self, audit_pool: Optional[asyncpg.Pool], rates: UsageRates):
        self._pool = audit_pool
        self._rates = rates

    @property
    def is_available(self) -> bool:
        return self._pool is not None

    async def record_events(self, events: Sequence[UsageEvent]) -> int:
        """Idempotently insert a batch; returns the count *actually* inserted.

        Snapshots the rate onto any event that doesn't already carry one (the
        emitter usually leaves rate/cost None). Re-emitted rows collide on the
        dedupe key and are skipped (counted out of the return value), so the
        at-least-once emitters can safely retry.
        """
        if self._pool is None or not events:
            return 0

        for e in events:
            if e.rate_usd is None and e.cost_usd is None:
                rate = await self._rates.resolve(e.category, e.resource, e.unit, e.ts)
                if rate is not None:
                    e.rate_usd = rate
                    e.cost_usd = Decimal(str(e.quantity)) * rate

        cols: Dict[str, list] = {
            "ts": [e.ts for e in events],
            "user_id": [_uuid(e.user_id) for e in events],
            "project_id": [_uuid(e.project_id) for e in events],
            "ref_kind": [e.ref_kind for e in events],
            "ref_id": [_uuid(e.ref_id) for e in events],
            "category": [e.category for e in events],
            "resource": [e.resource for e in events],
            "quantity": [_dec(e.quantity) for e in events],
            "unit": [e.unit for e in events],
            "rate_usd": [_dec(e.rate_usd) for e in events],
            "cost_usd": [_dec(e.cost_usd) for e in events],
            "source": [e.source for e in events],
            "source_id": [e.source_id for e in events],
            "details": [json.dumps(e.details or {}) for e in events],
        }
        try:
            async with self._pool.acquire() as conn:
                status = await conn.execute(_INSERT_SQL, *cols.values())
            # status is "INSERT 0 <n>" where n excludes ON CONFLICT skips.
            inserted = int(status.split()[-1])
            if inserted:
                logger.debug("usage ledger: +%d event(s)", inserted)
            return inserted
        except Exception:
            # One bad row (e.g. a ts outside any partition) must not sink the whole
            # batch — and since the poller re-sees the same rows, a sunk batch would
            # block EVERY future materialization. Fall back to per-row inserts so the
            # good rows land and only the offender is dropped.
            logger.warning(
                "usage ledger batch insert failed; retrying per-row", exc_info=True
            )
            return await self._insert_per_row(cols, events)

    async def _insert_per_row(
        self, cols: Dict[str, list], events: Sequence["UsageEvent"]
    ) -> int:
        """Fallback: insert each event alone so one uninsertable row is isolated."""
        inserted = 0
        for i in range(len(events)):
            one = {k: [v[i]] for k, v in cols.items()}
            try:
                async with self._pool.acquire() as conn:
                    status = await conn.execute(_INSERT_SQL, *one.values())
                inserted += int(status.split()[-1])
            except Exception:
                e = events[i]
                logger.warning(
                    "usage ledger: dropping uninsertable event source=%s "
                    "source_id=%s unit=%s (non-fatal)",
                    e.source,
                    e.source_id,
                    e.unit,
                    exc_info=True,
                )
        return inserted

    async def query_usage(
        self,
        *,
        from_ts: datetime,
        to_ts: datetime,
        owner_user_id: Optional[str] = None,
        visible_project_ids: Optional[Sequence[str]] = None,
        scope_project_id: Optional[str] = None,
        ref_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Aggregate usage in [from_ts, to_ts), grouped by (category, unit).

        Visibility (G5) mirrors the stats endpoints: an ``owner_user_id`` (a
        non-admin caller) restricts to rows they own or a project they can see;
        an admin passes neither (full fleet) or just ``scope_project_id``. Summing
        quantity is only meaningful within a unit, hence the (category, unit)
        grouping; ``cost_usd`` is summed across all rows for the headline total.
        """
        empty = {"by_category": [], "total_cost_usd": 0.0}
        if self._pool is None:
            return empty

        clauses = ["ts >= $1", "ts < $2"]
        params: List[Any] = [from_ts, to_ts]
        if owner_user_id is not None:
            params.append(_uuid(owner_user_id))
            own = f"user_id = ${len(params)}"
            pids = [_uuid(p) for p in (visible_project_ids or [])]
            if pids:
                params.append(pids)
                clauses.append(f"({own} OR project_id = ANY(${len(params)}::uuid[]))")
            else:
                clauses.append(own)
        elif scope_project_id is not None:
            params.append(_uuid(scope_project_id))
            clauses.append(f"project_id = ${len(params)}")
        if ref_id is not None:
            params.append(_uuid(ref_id))
            clauses.append(f"ref_id = ${len(params)}")

        sql = (
            "SELECT category, unit, SUM(quantity) AS quantity, "
            "COALESCE(SUM(cost_usd), 0) AS cost_usd, COUNT(*) AS events "
            f"FROM usage_events WHERE {' AND '.join(clauses)} "
            "GROUP BY category, unit ORDER BY category, unit"
        )
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(sql, *params)
        except Exception:
            logger.warning("usage ledger query failed (non-fatal)", exc_info=True)
            return empty

        by_category = [
            {
                "category": r["category"],
                "unit": r["unit"],
                "quantity": float(r["quantity"]) if r["quantity"] is not None else 0.0,
                "cost_usd": float(r["cost_usd"]),
                "events": r["events"],
            }
            for r in rows
        ]
        total = sum(item["cost_usd"] for item in by_category)
        return {"by_category": by_category, "total_cost_usd": total}


__all__ = ["UsageEvent", "UsageRates", "UsageLedger"]
