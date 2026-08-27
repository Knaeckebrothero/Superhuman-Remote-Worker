from __future__ import annotations

import asyncio
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestrator.services.infrastructure_metering.runtime import (
    InfrastructureMeteringRuntime,
    infrastructure_metering_runtime_loop,
)
from orchestrator.services.usage_ledger import StrictUsageLedgerError


@pytest.mark.asyncio
async def test_runtime_resumes_then_adopts_plans_before_planning_and_sealing() -> None:
    calls: list[str] = []

    class Cutover:
        async def status(self):
            return SimpleNamespace(
                state="preparing",
                phase="legacy-draining",
                request_id="request-1",
            )

        async def resume(self, generation: int, *, idempotency_key):
            assert idempotency_key == "request-1"
            calls.append(f"cutover:{generation}")
            return SimpleNamespace(
                progressed=True,
                status=SimpleNamespace(
                    state="preparing",
                    phase="legacy-draining",
                ),
            )

    class Materializer:
        pending = [object(), None, object(), None]

        async def publish_one(self, generation: int):
            calls.append(f"publish:{generation}")
            return self.pending.pop(0)

        async def plan_batch(self, generation: int):
            calls.append(f"plan:{generation}")
            return (object(),)

    class App:
        async def fetchval(self, query: str):
            assert "next-sealable-day" in query
            calls.append("candidate")
            return date(2026, 8, 5)

    class Sealer:
        async def seal_day(self, day: date, generation: int):
            calls.append(f"seal:{day}:{generation}")

    runtime = InfrastructureMeteringRuntime(
        App(),  # type: ignore[arg-type]
        cutover=Cutover(),  # type: ignore[arg-type]
        materializer=Materializer(),  # type: ignore[arg-type]
        sealer=Sealer(),  # type: ignore[arg-type]
        max_publications_per_cycle=10,
    )
    result = await runtime.run_cycle(12)

    assert calls == [
        "cutover:12",
        "publish:12",
        "publish:12",
        "plan:12",
        "publish:12",
        "publish:12",
        "candidate",
        "seal:2026-08-05:12",
    ]
    assert result.cutover_progressed is True
    assert result.plans_adopted == 1
    assert result.plans_created == 1
    assert result.plans_published == 2
    assert result.sealed_day == date(2026, 8, 5)


@pytest.mark.asyncio
async def test_runtime_does_not_grow_outbox_behind_audit_failure() -> None:
    class Materializer:
        def __init__(self) -> None:
            self.plan_batch = AsyncMock()
            self.calls = 0

        async def publish_one(self, generation: int):
            self.calls += 1
            if self.calls == 1:
                raise StrictUsageLedgerError("audit unavailable")
            return None

    materializer = Materializer()
    runtime = InfrastructureMeteringRuntime(
        SimpleNamespace(fetchval=AsyncMock(return_value=None)),  # type: ignore[arg-type]
        materializer=materializer,  # type: ignore[arg-type]
    )

    result = await runtime.run_cycle(3)

    assert result.publication_blocked == 1
    materializer.plan_batch.assert_not_awaited()


@pytest.mark.asyncio
async def test_runtime_loop_requires_tenure_generation() -> None:
    runtime = SimpleNamespace(run_cycle=AsyncMock())
    stop = asyncio.Event()

    with pytest.raises(RuntimeError, match="generation is unavailable"):
        await infrastructure_metering_runtime_loop(
            stop,
            runtime,  # type: ignore[arg-type]
            lambda: None,
            interval_seconds=0.01,
        )

    runtime.run_cycle.assert_not_awaited()
