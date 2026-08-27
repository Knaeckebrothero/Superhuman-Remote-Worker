"""Leader-owned Slice 1 publication, sealing, and cutover coordinator.

The individual services keep their own database generation fences.  This loop
only orders bounded work: resume an already-requested irreversible cutover,
adopt/replay frozen audit plans before creating new intent, then seal the first
eligible UTC day.  Typed rollup remains a separate leader loop and can only
cross a day after the sealer commits its proof.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
import logging
from typing import Any, Protocol

import asyncpg

from .materializer import (
    InfrastructureUsageMaterializer,
    PublicationConflictError,
    PublicationDisabledError,
    PublicationFenceError,
)
from .sealer import (
    DaySealingBlocked,
    DaySealingDisabled,
    DaySealingFenceError,
    InfrastructureUsageDaySealer,
)
from ..usage_ledger import (
    StrictUsageLedgerError,
)

logger = logging.getLogger(__name__)

_NEXT_DAY_SQL = """
/* infra-runtime:next-sealable-day */
WITH control AS (
    SELECT cutover_state, cutover_at,
           (statement_timestamp() AT TIME ZONE 'UTC')::date AS today
    FROM infra_metering_control
    WHERE singleton = TRUE
), candidate AS (
    SELECT generated.day::date AS day
    FROM control
    CROSS JOIN LATERAL generate_series(
        (control.cutover_at AT TIME ZONE 'UTC')::date,
        control.today - 1,
        INTERVAL '1 day'
    ) AS generated(day)
    LEFT JOIN infra_usage_day_state AS state
      ON state.day = generated.day::date
    WHERE control.cutover_state = 'active'
      AND control.cutover_at IS NOT NULL
      AND COALESCE(state.state, 'open') <> 'sealed'
    ORDER BY generated.day
    LIMIT 1
)
SELECT day FROM candidate
"""


class CutoverCoordinator(Protocol):
    """Narrow interface implemented by the crash-resumable cutover service."""

    async def status(self) -> Any: ...

    async def resume(self, generation: int, *, idempotency_key: Any) -> Any: ...


@dataclass(frozen=True, slots=True)
class MeteringRuntimeCycle:
    cutover_progressed: bool = False
    plans_adopted: int = 0
    plans_created: int = 0
    plans_published: int = 0
    publication_blocked: int = 0
    sealed_day: date | None = None


class InfrastructureMeteringRuntime:
    """Run one bounded, generation-fenced Slice 1 work cycle."""

    def __init__(
        self,
        app_pool: asyncpg.Pool,
        *,
        cutover: CutoverCoordinator | None = None,
        materializer: InfrastructureUsageMaterializer | None = None,
        sealer: InfrastructureUsageDaySealer | None = None,
        max_publications_per_cycle: int = 100,
    ) -> None:
        if not 1 <= max_publications_per_cycle <= 1_000:
            raise ValueError("publication cycle bound must be between 1 and 1000")
        self._app = app_pool
        self._cutover = cutover
        self._materializer = materializer
        self._sealer = sealer
        self._max_publications = max_publications_per_cycle

    async def run_cycle(self, generation: int) -> MeteringRuntimeCycle:
        if isinstance(generation, bool) or not isinstance(generation, int):
            raise ValueError("metering generation must be an integer")
        if generation <= 0:
            raise ValueError("metering generation must be positive")

        cutover_progressed = False
        if self._cutover is not None:
            before = await self._cutover.status()
            if getattr(before, "state", None) == "preparing":
                request_id = getattr(before, "request_id", None)
                if request_id is None:
                    raise RuntimeError("preparing cutover has no request identity")
                result = await self._cutover.resume(
                    generation,
                    idempotency_key=request_id,
                )
                after = getattr(result, "status", None)
                cutover_progressed = bool(getattr(result, "progressed", False)) or (
                    after is not None
                    and (
                        getattr(after, "state", None),
                        getattr(after, "phase", None),
                    )
                    != (
                        getattr(before, "state", None),
                        getattr(before, "phase", None),
                    )
                )

        adopted = published = blocked = 0
        created = 0
        if self._materializer is not None:
            # Always replay app-frozen intent first. This is the audit-commit /
            # app-CAS crash recovery path and prevents an outbox backlog from
            # growing while older intent is unresolved.
            for _ in range(self._max_publications):
                try:
                    result = await self._materializer.publish_one(generation)
                except StrictUsageLedgerError:
                    blocked += 1
                    # The materializer advances retry scheduling before raising;
                    # another plan may now be eligible, so keep the bounded pass
                    # moving instead of recreating head-of-line starvation here.
                    continue
                except PublicationConflictError:
                    blocked += 1
                    continue
                if result is None:
                    break
                adopted += 1
                published += 1

            if blocked == 0 and adopted < self._max_publications:
                try:
                    created = len(await self._materializer.plan_batch(generation))
                except PublicationConflictError:
                    blocked += 1

                remaining = self._max_publications - adopted - blocked
                for _ in range(max(0, remaining)):
                    try:
                        result = await self._materializer.publish_one(generation)
                    except StrictUsageLedgerError:
                        blocked += 1
                        continue
                    except PublicationConflictError:
                        blocked += 1
                        continue
                    if result is None:
                        break
                    published += 1

        sealed_day = None
        if self._sealer is not None:
            candidate = await self._app.fetchval(_NEXT_DAY_SQL)
            if candidate is not None:
                if not isinstance(candidate, date):
                    raise RuntimeError("next infrastructure seal candidate is invalid")
                try:
                    await self._sealer.seal_day(candidate, generation)
                except DaySealingBlocked as exc:
                    logger.info(
                        "infrastructure day sealing blocked day=%s reason=%s",
                        candidate,
                        str(exc),
                    )
                else:
                    sealed_day = candidate

        return MeteringRuntimeCycle(
            cutover_progressed=cutover_progressed,
            plans_adopted=adopted,
            plans_created=created,
            plans_published=published,
            publication_blocked=blocked,
            sealed_day=sealed_day,
        )


async def infrastructure_metering_runtime_loop(
    stop: asyncio.Event,
    runtime: InfrastructureMeteringRuntime,
    generation_provider: Callable[[], int | None],
    *,
    interval_seconds: float = 5.0,
) -> None:
    """Run bounded cycles for one advisory-lock tenure."""

    if interval_seconds <= 0:
        raise ValueError("infrastructure metering runtime interval must be positive")
    logger.info("infrastructure metering runtime starting")
    try:
        while not stop.is_set():
            generation = generation_provider()
            if generation is None:
                raise RuntimeError("metering leader generation is unavailable")
            try:
                result = await runtime.run_cycle(generation)
                if any(
                    (
                        result.cutover_progressed,
                        result.plans_created,
                        result.plans_published,
                        result.publication_blocked,
                        result.sealed_day is not None,
                    )
                ):
                    logger.info(
                        "infrastructure metering cycle cutover=%s created=%d "
                        "published=%d blocked=%d sealed_day=%s",
                        result.cutover_progressed,
                        result.plans_created,
                        result.plans_published,
                        result.publication_blocked,
                        result.sealed_day,
                    )
            except (
                PublicationDisabledError,
                PublicationFenceError,
                DaySealingDisabled,
                DaySealingFenceError,
            ) as exc:
                logger.info(
                    "infrastructure metering runtime fenced class=%s",
                    type(exc).__name__,
                )
            except Exception as exc:
                logger.warning(
                    "infrastructure metering cycle failed class=%s",
                    type(exc).__name__,
                    exc_info=True,
                )
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
            except TimeoutError:
                pass
    finally:
        logger.info("infrastructure metering runtime stopped")


__all__ = [
    "CutoverCoordinator",
    "InfrastructureMeteringRuntime",
    "MeteringRuntimeCycle",
    "infrastructure_metering_runtime_loop",
]
