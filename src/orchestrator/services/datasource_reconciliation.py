"""Durable datasource/project knowledge reconciliation.

Datasource policy writes enqueue a coalescing row in PostgreSQL.  This worker
applies the *current* junction state to the external knowledge stores rather
than treating queue rows as an operation log.  That makes delete/re-add races
and datasource/project deletion converge safely.

The database claim uses ``FOR UPDATE SKIP LOCKED`` and a lease, while the
success/retry writes are guarded by a sequence-backed, never-reused claim
token. The loop is also leader-gated to avoid needless duplicate external
writes during normal HA operation; the database guards remain the correctness
boundary during a brief dual-leader window.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

DatasourceSync = Callable[[str, dict[str, Any]], Awaitable[None]]
DatasourceDelete = Callable[[str, str], Awaitable[None]]

_DEFAULT_INTERVAL_SECONDS = 15.0
_DEFAULT_BATCH_SIZE = 50
_DEFAULT_LEASE_SECONDS = 120
_DEFAULT_BACKOFF_BASE_SECONDS = 5
_DEFAULT_BACKOFF_MAX_SECONDS = 3600


@dataclass(frozen=True)
class ReconciliationStats:
    """Outcome counters for one queue claim."""

    claimed: int = 0
    succeeded: int = 0
    failed: int = 0
    superseded: int = 0


def reconciliation_retry_delay(
    attempts: int,
    *,
    base_seconds: int = _DEFAULT_BACKOFF_BASE_SECONDS,
    max_seconds: int = _DEFAULT_BACKOFF_MAX_SECONDS,
) -> int:
    """Return a bounded exponential retry delay.

    ``attempts`` is the post-claim attempt count, so attempt one receives the
    base delay.  The exponent is capped before shifting to keep hostile or
    corrupt attempt counts cheap to evaluate.
    """

    base = max(1, int(base_seconds))
    ceiling = max(base, int(max_seconds))
    exponent = max(0, min(int(attempts) - 1, 30))
    return min(ceiling, base * (2**exponent))


def safe_reconciliation_error(exc: BaseException, *, operation: str) -> str:
    """Build persistence-safe failure text without copying exception details.

    Connector/driver exceptions commonly include a full DSN, response body, or
    configuration object.  Pattern-based redaction cannot prove those strings
    secret-free, so the durable queue deliberately records only the sanitized
    exception class and a fixed operation label.  Full exception strings are
    likewise not emitted by this module's logs.
    """

    error_class = re.sub(r"[^A-Za-z0-9_.-]", "_", type(exc).__name__)[:80]
    if not error_class:
        error_class = "ReconciliationError"
    safe_operation = re.sub(r"[^a-z0-9_-]", "_", operation.lower())[:40]
    return f"{error_class}: datasource knowledge {safe_operation} failed"


async def reconcile_datasource_projects_once(
    db: Any,
    *,
    sync_fn: DatasourceSync,
    delete_fn: DatasourceDelete,
    leader: bool,
    batch_size: int = _DEFAULT_BATCH_SIZE,
    lease_seconds: int = _DEFAULT_LEASE_SECONDS,
    backoff_base_seconds: int = _DEFAULT_BACKOFF_BASE_SECONDS,
    backoff_max_seconds: int = _DEFAULT_BACKOFF_MAX_SECONDS,
) -> ReconciliationStats:
    """Claim and process one batch of datasource/project reconciliation rows.

    ``sync_fn`` and ``delete_fn`` must be idempotent and must raise when an
    external store did not converge.  A missing link (including either side
    having been deleted) is authoritative delete state.
    """

    if not leader:
        return ReconciliationStats()

    rows = await db.claim_datasource_project_reconciliations(
        limit=max(1, int(batch_size)),
        lease_seconds=max(10, int(lease_seconds)),
    )
    succeeded = 0
    failed = 0
    superseded = 0

    for row in rows:
        project_id = str(row["project_id"])
        datasource_id = str(row["datasource_id"])
        revision = int(row["policy_revision"])
        claim_token = int(row["claim_token"])
        attempts = max(1, int(row.get("attempts") or 1))
        operation = "state_read"

        try:
            datasource = await _read_linked_datasource(
                db,
                project_id=project_id,
                datasource_id=datasource_id,
            )
            if datasource is None:
                operation = "delete"
                await delete_fn(project_id, datasource_id)
            else:
                operation = "sync"
                await sync_fn(project_id, datasource)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failed += 1
            safe_error = safe_reconciliation_error(exc, operation=operation)
            delay = reconciliation_retry_delay(
                attempts,
                base_seconds=backoff_base_seconds,
                max_seconds=backoff_max_seconds,
            )
            try:
                retained = await db.retry_datasource_project_reconciliation(
                    project_id,
                    datasource_id,
                    claim_token,
                    safe_error=safe_error,
                    delay_seconds=delay,
                )
            except asyncio.CancelledError:
                raise
            except Exception as retry_exc:
                # The claim lease makes the row due again even if the retry
                # bookkeeping write loses a transient database connection.
                logger.warning(
                    "datasource reconciliation retry bookkeeping failed "
                    "project=%s datasource=%s error_class=%s",
                    project_id,
                    datasource_id,
                    type(retry_exc).__name__,
                )
            else:
                if not retained:
                    superseded += 1
            logger.warning(
                "datasource reconciliation deferred project=%s datasource=%s "
                "policy_revision=%d claim_token=%d attempt=%d delay=%ds error=%s",
                project_id,
                datasource_id,
                revision,
                claim_token,
                attempts,
                delay,
                safe_error,
            )
            continue

        try:
            removed = await db.finish_datasource_project_reconciliation(
                project_id,
                datasource_id,
                claim_token,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # The external operation is idempotent and the lease will expire,
            # so leaving the row is safer than acknowledging it ambiguously.
            failed += 1
            logger.warning(
                "datasource reconciliation acknowledgement failed "
                "project=%s datasource=%s policy_revision=%d claim_token=%d "
                "error_class=%s",
                project_id,
                datasource_id,
                revision,
                claim_token,
                type(exc).__name__,
            )
        else:
            if removed:
                succeeded += 1
            else:
                # A newer event replaced (or already acknowledged) the claimed
                # generation. The guarded finish atomically enqueues a fresh
                # current-state pass, so this stale external write is repaired
                # even when the newer worker deleted the prior queue row first.
                superseded += 1

    return ReconciliationStats(
        claimed=len(rows),
        succeeded=succeeded,
        failed=failed,
        superseded=superseded,
    )


async def run_datasource_project_reconciler(
    db: Any,
    shutdown_event: asyncio.Event,
    is_leader_fn: Callable[[], bool],
    *,
    sync_fn: DatasourceSync,
    delete_fn: DatasourceDelete,
    interval_seconds: float | None = None,
    batch_size: int | None = None,
    lease_seconds: int | None = None,
) -> None:
    """Drain the durable queue until shutdown, processing only while leader.

    Pass the live leader predicate (normally ``is_leader.is_set``), rather than
    a captured boolean.  Full batches are drained without waiting for another
    interval; each pass re-checks leadership and yields to the event loop.
    """

    interval = _positive_float_env(
        "DATASOURCE_RECONCILE_INTERVAL_SECONDS",
        _DEFAULT_INTERVAL_SECONDS,
        override=interval_seconds,
    )
    limit = _positive_int_env(
        "DATASOURCE_RECONCILE_BATCH_SIZE",
        _DEFAULT_BATCH_SIZE,
        minimum=1,
        maximum=500,
        override=batch_size,
    )
    lease = _positive_int_env(
        "DATASOURCE_RECONCILE_LEASE_SECONDS",
        _DEFAULT_LEASE_SECONDS,
        minimum=10,
        maximum=3600,
        override=lease_seconds,
    )
    logger.info(
        "Datasource project reconciler started (interval=%.1fs batch=%d lease=%ds)",
        interval,
        limit,
        lease,
    )

    while not shutdown_event.is_set():
        leader = False
        try:
            leader = bool(is_leader_fn())
            stats = await reconcile_datasource_projects_once(
                db,
                sync_fn=sync_fn,
                delete_fn=delete_fn,
                leader=leader,
                batch_size=limit,
                lease_seconds=lease,
            )
            if stats.succeeded or stats.failed or stats.superseded:
                logger.info(
                    "Datasource project reconciliation batch claimed=%d "
                    "succeeded=%d failed=%d superseded=%d",
                    stats.claimed,
                    stats.succeeded,
                    stats.failed,
                    stats.superseded,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Do not print database/driver messages: those can carry DSNs.
            logger.warning(
                "Datasource project reconciliation tick failed error_class=%s",
                type(exc).__name__,
            )
            stats = ReconciliationStats()

        if stats.claimed >= limit and leader:
            await asyncio.sleep(0)
            continue
        if await _wait_for_shutdown(shutdown_event, interval):
            break

    logger.info("Datasource project reconciler stopped")


async def _read_linked_datasource(
    db: Any,
    *,
    project_id: str,
    datasource_id: str,
) -> dict[str, Any] | None:
    """Read the current linked descriptor, including project overrides.

    ``project_datasources`` has cascading foreign keys to both authoritative
    rows.  Consequently, an empty result represents an unlinked datasource, a
    deleted datasource, or a deleted project; all three require external
    deletion.  The queue intentionally has no corresponding foreign keys so
    this tombstone work survives either deletion.
    """

    rows: Sequence[Mapping[str, Any]] = await db.list_project_datasources(project_id)
    for datasource in rows:
        if str(datasource.get("id")) == datasource_id:
            return dict(datasource)
    return None


async def _wait_for_shutdown(
    shutdown_event: asyncio.Event,
    timeout: float,
) -> bool:
    try:
        await asyncio.wait_for(shutdown_event.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        return False
    return True


def _positive_int_env(
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
    override: int | None,
) -> int:
    raw: object = override if override is not None else os.getenv(name, str(default))
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _positive_float_env(
    name: str,
    default: float,
    *,
    override: float | None,
) -> float:
    raw: object = override if override is not None else os.getenv(name, str(default))
    try:
        parsed = float(raw)
    except (TypeError, ValueError):
        parsed = default
    return max(0.05, min(parsed, 3600.0))


__all__ = [
    "DatasourceDelete",
    "DatasourceSync",
    "ReconciliationStats",
    "reconcile_datasource_projects_once",
    "reconciliation_retry_delay",
    "run_datasource_project_reconciler",
    "safe_reconciliation_error",
]
