"""Independent completion-finalizer liveness and age monitoring.

This sampler intentionally does not run inside the finalizer leader loop: an
executor outage must not disable the alarm that reports it.  Parked commands
remain unfinalized operator work and are therefore included in oldest-age.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, AsyncIterator, Awaitable, Callable, Literal

logger = logging.getLogger(__name__)

ZERO_FINALIZER_LEADER_DEDUP_KEY = "completion.finalizer.zero_leader"
OLDEST_UNFINALIZED_COMMAND_DEDUP_KEY = "completion.command.oldest_unfinalized"
FINALIZER_LEASE_NAME = "job_completion"

MonitorAlertKind = Literal["zero_finalizer_leader", "oldest_unfinalized_command"]


@dataclass(frozen=True, slots=True)
class CompletionMonitorSample:
    observed_at: datetime
    live_finalizer_leaders: int
    oldest_command_id: str | None
    oldest_job_id: str | None
    oldest_state: str | None
    oldest_reported_at: datetime | None
    oldest_age_seconds: float | None


@dataclass(frozen=True, slots=True)
class CompletionMonitorAlert:
    kind: MonitorAlertKind
    dedup_key: str
    message: str
    observed_at: datetime
    command_id: str | None = None
    job_id: str | None = None
    command_state: str | None = None
    age_seconds: float | None = None


MonitorAlertCallback = Callable[[CompletionMonitorAlert], Awaitable[None] | None]


@asynccontextmanager
async def _connection(source: Any) -> AsyncIterator[Any]:
    acquire = getattr(source, "acquire", None)
    if acquire is None:
        yield source
        return
    async with acquire() as conn:
        yield conn


class CompletionMonitor:
    """Sample and report completion liveness with fixed-cardinality keys."""

    def __init__(
        self,
        db: Any,
        alert: MonitorAlertCallback,
        *,
        max_unfinalized_age_seconds: float = 30 * 60,
        startup_grace_seconds: float = 30,
        poll_seconds: float = 30,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not callable(alert):
            raise ValueError("alert must be callable")
        if float(max_unfinalized_age_seconds) <= 0:
            raise ValueError("max_unfinalized_age_seconds must be positive")
        if float(startup_grace_seconds) < 0:
            raise ValueError("startup_grace_seconds cannot be negative")
        if float(poll_seconds) <= 0:
            raise ValueError("poll_seconds must be positive")
        self.db = db
        self.alert = alert
        self.max_unfinalized_age_seconds = float(max_unfinalized_age_seconds)
        self.startup_grace_seconds = float(startup_grace_seconds)
        self.poll_seconds = float(poll_seconds)
        self.clock = clock
        self._started_at = float(clock())

    async def sample(self) -> CompletionMonitorSample:
        """Read live leader count and the oldest unfinished row by DB time."""

        async with _connection(self.db) as conn:
            row = await conn.fetchrow(
                """
                SELECT clock_timestamp() AS observed_at,
                       (
                           SELECT count(*)::int
                           FROM completion_finalizer_leases AS lease
                           WHERE lease.lease_name=$1::text
                             AND lease.expires_at > clock_timestamp()
                       ) AS live_finalizer_leaders,
                       oldest.id AS oldest_command_id,
                       oldest.job_id AS oldest_job_id,
                       oldest.state AS oldest_state,
                       oldest.reported_at AS oldest_reported_at,
                       CASE WHEN oldest.reported_at IS NULL THEN NULL
                            ELSE GREATEST(
                                0.0,
                                extract(epoch FROM (
                                    clock_timestamp()-oldest.reported_at
                                ))
                            )::float8
                       END AS oldest_age_seconds
                FROM (VALUES (1)) AS singleton(value)
                LEFT JOIN LATERAL (
                    SELECT command.id, command.job_id, command.state,
                           command.reported_at, command.report_seq
                    FROM job_completion_commands AS command
                    WHERE command.state IN ('pending','finalizing','parked')
                    ORDER BY command.reported_at, command.job_id,
                             command.report_seq
                    LIMIT 1
                ) AS oldest ON true
                """,
                FINALIZER_LEASE_NAME,
            )
        return CompletionMonitorSample(
            observed_at=row["observed_at"],
            live_finalizer_leaders=int(row["live_finalizer_leaders"] or 0),
            oldest_command_id=(
                str(row["oldest_command_id"])
                if row["oldest_command_id"] is not None
                else None
            ),
            oldest_job_id=(
                str(row["oldest_job_id"]) if row["oldest_job_id"] is not None else None
            ),
            oldest_state=(
                str(row["oldest_state"]) if row["oldest_state"] is not None else None
            ),
            oldest_reported_at=row["oldest_reported_at"],
            oldest_age_seconds=(
                float(row["oldest_age_seconds"])
                if row["oldest_age_seconds"] is not None
                else None
            ),
        )

    def alerts_for(
        self, sample: CompletionMonitorSample
    ) -> tuple[CompletionMonitorAlert, ...]:
        """Pure threshold evaluation with one bounded key per alarm class."""

        if float(self.clock()) - self._started_at < self.startup_grace_seconds:
            return ()
        alerts: list[CompletionMonitorAlert] = []
        if sample.live_finalizer_leaders == 0:
            alerts.append(
                CompletionMonitorAlert(
                    kind="zero_finalizer_leader",
                    dedup_key=ZERO_FINALIZER_LEADER_DEDUP_KEY,
                    message="no live job-completion finalizer leader lease",
                    observed_at=sample.observed_at,
                )
            )
        if (
            sample.oldest_age_seconds is not None
            and sample.oldest_age_seconds >= self.max_unfinalized_age_seconds
        ):
            alerts.append(
                CompletionMonitorAlert(
                    kind="oldest_unfinalized_command",
                    dedup_key=OLDEST_UNFINALIZED_COMMAND_DEDUP_KEY,
                    message=(
                        "oldest unfinished completion command is "
                        f"{sample.oldest_age_seconds:.1f}s old"
                    ),
                    observed_at=sample.observed_at,
                    command_id=sample.oldest_command_id,
                    job_id=sample.oldest_job_id,
                    command_state=sample.oldest_state,
                    age_seconds=sample.oldest_age_seconds,
                )
            )
        return tuple(alerts)

    async def run_once(self) -> tuple[CompletionMonitorAlert, ...]:
        sample = await self.sample()
        alerts = self.alerts_for(sample)
        for alert in alerts:
            result = self.alert(alert)
            if inspect.isawaitable(result):
                await result
        return alerts

    async def run(self, shutdown_event: asyncio.Event) -> None:
        """Poll independently; transient sample/sink failures do not kill it."""

        while not shutdown_event.is_set():
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("completion monitor tick failed")
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=self.poll_seconds)
            except TimeoutError:
                pass


__all__ = [
    "CompletionMonitor",
    "CompletionMonitorAlert",
    "CompletionMonitorSample",
    "FINALIZER_LEASE_NAME",
    "OLDEST_UNFINALIZED_COMMAND_DEDUP_KEY",
    "ZERO_FINALIZER_LEADER_DEDUP_KEY",
]
