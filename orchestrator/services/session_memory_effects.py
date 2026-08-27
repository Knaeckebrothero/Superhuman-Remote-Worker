"""Independent drain for durable stateless-session memory obligations.

The final transcript transaction produces one
``completion_effects(producer_kind='session_turn')`` row per accepted turn.
This module consumes only that producer kind; the job-completion finalizer and
its command rows remain a separate ownership domain.

The database lease serializes callbacks while their exact owner can renew.
Executors receive both the stable ``(producer_id, effect_name)`` identity and
an async authority permit tied to that owner. The session-memory executor uses
the permit around its vector-side transaction and commits a durable
destination ledger with every memory mutation. A crash after that commit but
before this app-DB receipt is therefore a receipt replay, not a repeated vector
write.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import random
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Awaitable, Callable, Mapping
from uuid import uuid4

logger = logging.getLogger(__name__)

AuthorityPermit = Callable[[], Awaitable[None]]

SESSION_MEMORY_EFFECT_NAME = "final_memory_extraction"
SESSION_MEMORY_EFFECT_GROUP = "memory_extraction"
SESSION_MEMORY_EFFECT_DETAIL_LIMIT_BYTES = 8 * 1024

CLAIM_BATCH = 20
LEASE_SECONDS = 120.0
HEARTBEAT_SECONDS = 20.0
IDLE_POLL_SECONDS = 1.0
BUSY_POLL_SECONDS = 0.1
RETRY_BASE_SECONDS = 5.0
RETRY_MAX_SECONDS = 300.0
DONE_RETENTION_SECONDS = 24 * 60 * 60
DEAD_RETENTION_SECONDS = 7 * 24 * 60 * 60
PRUNE_BATCH = 1000
PRUNE_EVERY_SECONDS = 60.0


class SessionMemoryEffectError(RuntimeError):
    """Base class for session-memory drain failures."""


class SessionMemoryEffectPermanentError(SessionMemoryEffectError):
    """An executor input/configuration failure that should not be retried."""


class SessionMemoryEffectLeaseLost(SessionMemoryEffectError):
    """The exact DB-clock claim no longer authorizes this executor."""


@dataclass(frozen=True, slots=True)
class SessionMemoryEffect:
    """One exact claimed final-memory obligation."""

    producer_id: str
    scope_id: str | None
    effect_name: str
    effect_group: str
    attempts: int
    max_attempts: int
    created_at: datetime | None
    complete_by: datetime | None
    detail: dict[str, Any]
    authority_permit: AuthorityPermit | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "SessionMemoryEffect":
        raw_detail = row.get("detail")
        if isinstance(raw_detail, str):
            try:
                raw_detail = json.loads(raw_detail)
            except (TypeError, ValueError) as exc:
                raise SessionMemoryEffectPermanentError(
                    "session memory effect detail is malformed"
                ) from exc
        if not isinstance(raw_detail, Mapping):
            raise SessionMemoryEffectPermanentError(
                "session memory effect detail is not an object"
            )
        name = str(row.get("effect_name") or "")
        group = str(row.get("effect_group") or "")
        if name != SESSION_MEMORY_EFFECT_NAME or group != SESSION_MEMORY_EFFECT_GROUP:
            raise SessionMemoryEffectPermanentError(
                "session memory effect stable identity is unsupported"
            )
        return cls(
            producer_id=str(row["producer_id"]),
            scope_id=(str(row["scope_id"]) if row.get("scope_id") else None),
            effect_name=name,
            effect_group=group,
            attempts=int(row.get("attempts") or 0),
            max_attempts=int(row.get("max_attempts") or 0),
            created_at=row.get("created_at"),
            complete_by=row.get("complete_by"),
            detail=dict(raw_detail),
        )

    @property
    def idempotency_key(self) -> str:
        """Stable key for a destination-side dedup ledger."""

        return f"session_turn:{self.producer_id}:{self.effect_name}"


@dataclass(frozen=True, slots=True)
class SessionMemoryDrainResult:
    """Observable outcome counters for one bounded drain pass."""

    claimed: int = 0
    done: int = 0
    retried: int = 0
    dead: int = 0
    lease_lost: int = 0


EffectExecutor = Callable[[SessionMemoryEffect], Awaitable[Mapping[str, Any] | None]]


def _error_code(exc: BaseException) -> str:
    return (type(exc).__name__ or "session_memory_effect_error")[:128]


def _retry_delay(
    attempt: int,
    *,
    random_source: Callable[[], float],
    base_seconds: float,
    max_seconds: float,
) -> float:
    # Shared run_queue dialect: linear 5s × attempts with bounded jitter. A
    # session effect is one small auxiliary unit; exponential growth would
    # turn its five-attempt budget into an unnecessarily long final-memory gap.
    nominal = min(max_seconds, base_seconds * max(1, int(attempt)))
    jitter = max(0.0, min(0.2, float(random_source()) * 0.2))
    return nominal * (1.0 + jitter)


def _completed_detail(
    effect: SessionMemoryEffect,
    output: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if output is not None and not isinstance(output, Mapping):
        raise SessionMemoryEffectPermanentError(
            "session memory executor output must be an object or None"
        )
    result = dict(effect.detail)
    result["output"] = dict(output or {})
    encoded = json.dumps(
        result,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
        allow_nan=False,
    )
    if len(encoded.encode("utf-8")) > SESSION_MEMORY_EFFECT_DETAIL_LIMIT_BYTES:
        raise SessionMemoryEffectPermanentError(
            "session memory effect replay detail exceeds 8 KiB"
        )
    return result


class SessionMemoryEffectDrain:
    """Lease, execute and settle final-memory effects independently of turns."""

    def __init__(
        self,
        db: Any,
        executor: EffectExecutor,
        *,
        claim_batch: int = CLAIM_BATCH,
        lease_seconds: float = LEASE_SECONDS,
        heartbeat_seconds: float = HEARTBEAT_SECONDS,
        retry_base_seconds: float = RETRY_BASE_SECONDS,
        retry_max_seconds: float = RETRY_MAX_SECONDS,
        idle_poll_seconds: float = IDLE_POLL_SECONDS,
        busy_poll_seconds: float = BUSY_POLL_SECONDS,
        random_source: Callable[[], float] = random.random,
    ) -> None:
        if not 1 <= int(claim_batch) <= 1000:
            raise ValueError("session memory claim batch must be 1..1000")
        numeric = (
            lease_seconds,
            heartbeat_seconds,
            retry_base_seconds,
            retry_max_seconds,
            idle_poll_seconds,
            busy_poll_seconds,
        )
        if any(
            not math.isfinite(float(value)) or float(value) <= 0 for value in numeric
        ):
            raise ValueError("session memory drain timings must be positive")
        if float(heartbeat_seconds) >= float(lease_seconds):
            raise ValueError("session memory heartbeat must be shorter than its lease")
        if float(retry_max_seconds) < float(retry_base_seconds):
            raise ValueError("session memory retry cap must cover its base")
        self._db = db
        self._executor = executor
        self._claim_batch = int(claim_batch)
        self._lease_seconds = float(lease_seconds)
        self._heartbeat_seconds = float(heartbeat_seconds)
        self._retry_base_seconds = float(retry_base_seconds)
        self._retry_max_seconds = float(retry_max_seconds)
        self._idle_poll_seconds = float(idle_poll_seconds)
        self._busy_poll_seconds = float(busy_poll_seconds)
        self._random_source = random_source

    async def _heartbeat(
        self,
        effect: SessionMemoryEffect,
        *,
        owner: str,
        stop: asyncio.Event,
        lost: asyncio.Event,
    ) -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._heartbeat_seconds)
                return
            except TimeoutError:
                pass
            try:
                renewed = await self._db.renew_session_memory_effect(
                    producer_id=effect.producer_id,
                    effect_name=effect.effect_name,
                    claimed_by=owner,
                    lease_seconds=self._lease_seconds,
                )
            except Exception:
                logger.exception(
                    "session memory effect heartbeat failed for %s",
                    effect.producer_id,
                )
                lost.set()
                return
            if not renewed:
                lost.set()
                return

    @staticmethod
    async def _stop_heartbeat(stop: asyncio.Event, task: asyncio.Task[None]) -> None:
        stop.set()
        await task

    async def _release_failure(
        self,
        effect: SessionMemoryEffect,
        *,
        owner: str,
        exc: BaseException,
        permanent: bool,
    ) -> str:
        backoff = _retry_delay(
            effect.attempts,
            random_source=self._random_source,
            base_seconds=self._retry_base_seconds,
            max_seconds=self._retry_max_seconds,
        )
        state = await self._db.retry_session_memory_effect(
            producer_id=effect.producer_id,
            effect_name=effect.effect_name,
            claimed_by=owner,
            error_code=_error_code(exc),
            backoff_seconds=backoff,
            force_dead=permanent,
        )
        if state == "dead":
            logger.error(
                "session memory effect %s exhausted or cannot be retried (%s)",
                effect.producer_id,
                _error_code(exc),
            )
            return "dead"
        if state == "pending":
            logger.warning(
                "session memory effect %s deferred after attempt %s (%s)",
                effect.producer_id,
                effect.attempts,
                _error_code(exc),
            )
            return "retried"
        return "lease_lost"

    async def _require_live_claim(
        self, effect: SessionMemoryEffect, *, owner: str
    ) -> None:
        """Prove the claim is still live immediately before external work."""

        try:
            renewed = await self._db.renew_session_memory_effect(
                producer_id=effect.producer_id,
                effect_name=effect.effect_name,
                claimed_by=owner,
                lease_seconds=self._lease_seconds,
            )
        except Exception as exc:
            raise SessionMemoryEffectLeaseLost(
                f"session memory effect {effect.producer_id} could not prove its claim"
            ) from exc
        if not renewed:
            raise SessionMemoryEffectLeaseLost(
                f"session memory effect {effect.producer_id} lost its claim"
            )

    async def _execute_claimed(self, row: Mapping[str, Any], *, owner: str) -> str:
        try:
            effect = SessionMemoryEffect.from_row(row)
        except SessionMemoryEffectPermanentError as exc:
            # Claim SQL only returns the stable effect name/group, so these ids
            # remain safe to use when retiring malformed producer detail.
            malformed = SessionMemoryEffect(
                producer_id=str(row["producer_id"]),
                scope_id=(str(row["scope_id"]) if row.get("scope_id") else None),
                effect_name=str(row["effect_name"]),
                effect_group=str(row["effect_group"]),
                attempts=int(row.get("attempts") or 0),
                max_attempts=int(row.get("max_attempts") or 0),
                created_at=row.get("created_at"),
                complete_by=row.get("complete_by"),
                detail={},
            )
            return await self._release_failure(
                malformed, owner=owner, exc=exc, permanent=True
            )

        try:
            await self._require_live_claim(effect, owner=owner)
        except SessionMemoryEffectLeaseLost:
            return "lease_lost"
        except Exception:
            logger.exception(
                "session memory effect pre-execution lease check failed for %s",
                effect.producer_id,
            )
            return "lease_lost"

        async def authority_permit() -> None:
            # Heartbeats and permits use the same exact owner and DB-clock
            # live-term CAS. Concurrent renewals can only extend this claim;
            # neither can revive a replaced or expired claimant.
            await self._require_live_claim(effect, owner=owner)

        effect = replace(effect, authority_permit=authority_permit)

        stop = asyncio.Event()
        lost = asyncio.Event()
        heartbeat = asyncio.create_task(
            self._heartbeat(effect, owner=owner, stop=stop, lost=lost),
            name=f"session-memory-effect-heartbeat-{effect.producer_id[:8]}",
        )
        try:
            output = await self._executor(effect)
            detail = _completed_detail(effect, output)
        except asyncio.CancelledError:
            await self._stop_heartbeat(stop, heartbeat)
            raise
        except SessionMemoryEffectPermanentError as exc:
            await self._stop_heartbeat(stop, heartbeat)
            if lost.is_set():
                return "lease_lost"
            return await self._release_failure(
                effect, owner=owner, exc=exc, permanent=True
            )
        except SessionMemoryEffectLeaseLost:
            await self._stop_heartbeat(stop, heartbeat)
            return "lease_lost"
        except Exception as exc:
            await self._stop_heartbeat(stop, heartbeat)
            if lost.is_set():
                return "lease_lost"
            return await self._release_failure(
                effect, owner=owner, exc=exc, permanent=False
            )

        await self._stop_heartbeat(stop, heartbeat)
        if lost.is_set():
            return "lease_lost"
        try:
            settled = await self._db.finish_session_memory_effect(
                producer_id=effect.producer_id,
                effect_name=effect.effect_name,
                claimed_by=owner,
                detail=detail,
            )
        except Exception:
            # The callback may have succeeded.  Leave the row claimed until its
            # DB-clock lease expires; a successor retries with the stable key.
            logger.exception(
                "session memory effect receipt failed for %s",
                effect.producer_id,
            )
            return "lease_lost"
        if not settled:
            # Keep a typed ownership-loss signal available to callers and
            # debuggers even though the continuous drain reduces it to a
            # counter so one stale row cannot fail the whole batch.
            lost_error = SessionMemoryEffectLeaseLost(
                f"session memory effect {effect.producer_id} lost its settle claim"
            )
            logger.warning("%s", lost_error)
            return "lease_lost"
        return "done"

    async def drain_once(self) -> SessionMemoryDrainResult:
        """Execute one bounded claim batch and return observable counters."""

        # Fresh per pass is the ABA fence; see PostgresDB's claim contract.
        owner = str(uuid4())
        rows = await self._db.claim_session_memory_effects(
            claimed_by=owner,
            limit=self._claim_batch,
            lease_seconds=self._lease_seconds,
        )
        if not rows:
            return SessionMemoryDrainResult()
        outcomes = await asyncio.gather(
            *(self._execute_claimed(row, owner=owner) for row in rows)
        )
        return SessionMemoryDrainResult(
            claimed=len(rows),
            done=outcomes.count("done"),
            retried=outcomes.count("retried"),
            dead=outcomes.count("dead"),
            lease_lost=outcomes.count("lease_lost"),
        )

    async def prune_once(
        self,
        *,
        batch_limit: int = PRUNE_BATCH,
        done_retention_seconds: float = DONE_RETENTION_SECONDS,
        dead_retention_seconds: float = DEAD_RETENTION_SECONDS,
    ) -> int:
        """Prune one short terminal-retention batch."""

        return await self._db.prune_session_memory_effects(
            batch_limit=batch_limit,
            done_retention_seconds=done_retention_seconds,
            dead_retention_seconds=dead_retention_seconds,
        )

    async def run_drain(self, shutdown_event: asyncio.Event) -> None:
        """Continuously drain independently until orchestrator shutdown."""

        loop = asyncio.get_running_loop()
        next_prune = loop.time()
        while not shutdown_event.is_set():
            try:
                result = await self.drain_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("session memory effect drain pass failed")
                result = SessionMemoryDrainResult()

            now = loop.time()
            if now >= next_prune:
                try:
                    await self.prune_once()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("session memory effect prune pass failed")
                next_prune = now + PRUNE_EVERY_SECONDS

            delay = (
                self._busy_poll_seconds if result.claimed else self._idle_poll_seconds
            )
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=delay)
            except TimeoutError:
                pass
