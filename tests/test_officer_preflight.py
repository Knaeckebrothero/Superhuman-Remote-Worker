"""BP-07: strict Officer jobs stay parked until durable activation."""

from __future__ import annotations

import asyncio
import copy
import uuid

import pytest

from orchestrator.services.job_provisioning import JobProvisioningError
from orchestrator.services.officer_preflight import (
    ensure_officer_job_activated,
    initial_preflight_context,
    initial_preflight_freeze,
)


class _PreflightDB:
    def __init__(self) -> None:
        self.job = {
            "id": str(uuid.uuid4()),
            "status": "paused",
            "assigned_agent_id": None,
            "context": {"provisioning_preflight": initial_preflight_context()},
            "freeze_data": initial_preflight_freeze(),
        }
        self._lock = asyncio.Lock()
        self.token: str | None = None
        self.finish_calls: list[dict] = []

    async def claim_officer_job_preflight(self, job_id, **kwargs):
        async with self._lock:
            state = self.job["context"]["provisioning_preflight"]["state"]
            if state not in {"not-attempted", "retryable-failed"}:
                return None
            self.token = str(uuid.uuid4())
            self.job["context"]["provisioning_preflight"].update(
                {"state": "in-progress", "attempt_token": self.token}
            )
            return {**copy.deepcopy(self.job), "preflight_attempt_token": self.token}

    async def finish_officer_job_preflight(self, job_id, **kwargs):
        if kwargs["attempt_token"] != self.token:
            return False
        self.finish_calls.append(dict(kwargs))
        preflight = self.job["context"]["provisioning_preflight"]
        if kwargs["activated"]:
            preflight.update({"state": "activated", "attempt_token": None})
            self.job.update({"status": "created", "freeze_data": None})
        else:
            preflight.update(
                {
                    "state": (
                        "retryable-failed"
                        if kwargs["retryable"]
                        else "permanent-failed"
                    ),
                    "phase": kwargs["phase"],
                    "failure_class": kwargs["failure_class"],
                    "error": kwargs["error"],
                    "attempt_token": None,
                }
            )
        return True

    async def get_job(self, job_id):
        return copy.deepcopy(self.job)

    async def dispatcher_can_claim(self):
        return self.job["status"] == "created" and self.job["freeze_data"] is None

    def expire_crashed_attempt(self):
        self.job["context"]["provisioning_preflight"]["state"] = "retryable-failed"


@pytest.mark.asyncio
async def test_delayed_provisioning_survives_repeated_dispatcher_polls():
    db = _PreflightDB()
    started = asyncio.Event()
    release = asyncio.Event()

    async def provision(_job, *, category=None):
        started.set()
        await release.wait()

    task = asyncio.create_task(
        ensure_officer_job_activated(db, db.job, provision=provision)
    )
    await started.wait()
    assert [await db.dispatcher_can_claim() for _ in range(5)] == [False] * 5
    release.set()
    outcome = await task
    assert outcome.activated is True
    assert await db.dispatcher_can_claim() is True


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["repository", "cloud"])
async def test_infrastructure_failures_are_classified_and_stay_parked(phase):
    db = _PreflightDB()

    async def provision(_job, *, category=None):
        raise JobProvisioningError(f"{phase} unavailable", phase=phase, retryable=True)

    outcome = await ensure_officer_job_activated(db, db.job, provision=provision)
    assert outcome.state == "retryable-failed"
    assert db.job["status"] == "paused"
    assert db.job["freeze_data"]["freeze_type"] == "officer_preflight"
    assert db.finish_calls[-1]["failure_class"] == "infrastructure"
    assert db.finish_calls[-1]["phase"] == phase


@pytest.mark.asyncio
async def test_permanent_preflight_failure_is_visible_and_not_reclaimed():
    db = _PreflightDB()
    calls = 0

    async def provision(_job, *, category=None):
        nonlocal calls
        calls += 1
        raise JobProvisioningError(
            "repository policy refuses this job",
            phase="repository",
            retryable=False,
        )

    failed = await ensure_officer_job_activated(db, db.job, provision=provision)
    repeated = await ensure_officer_job_activated(db, db.job, provision=provision)

    assert failed.state == "permanent-failed"
    assert failed.retryable is False
    assert repeated.state == "permanent-failed"
    assert repeated.attempted is False
    assert calls == 1


@pytest.mark.asyncio
async def test_crash_before_activation_retries_idempotent_external_effect_once():
    db = _PreflightDB()
    effects: set[str] = set()

    async def provision(job, *, category=None):
        effects.add(str(job["id"]))

    def fault(step):
        if step == "after_provisioning_before_activation":
            raise RuntimeError("process lost")

    with pytest.raises(RuntimeError, match="process lost"):
        await ensure_officer_job_activated(
            db, db.job, provision=provision, fault_injector=fault
        )
    assert await db.dispatcher_can_claim() is False
    db.expire_crashed_attempt()
    outcome = await ensure_officer_job_activated(db, db.job, provision=provision)
    assert outcome.activated is True
    assert effects == {db.job["id"]}


@pytest.mark.asyncio
async def test_crash_after_activation_does_not_repeat_provisioning():
    db = _PreflightDB()
    calls = 0

    async def provision(_job, *, category=None):
        nonlocal calls
        calls += 1

    def fault(step):
        if step == "after_activation":
            raise RuntimeError("process lost")

    with pytest.raises(RuntimeError, match="process lost"):
        await ensure_officer_job_activated(
            db, db.job, provision=provision, fault_injector=fault
        )
    recovered = await ensure_officer_job_activated(db, db.job, provision=provision)
    assert recovered.activated is True
    assert recovered.attempted is False
    assert calls == 1


@pytest.mark.asyncio
async def test_concurrent_activation_attempts_have_one_lease_and_one_effect():
    db = _PreflightDB()
    calls = 0

    async def provision(_job, *, category=None):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)

    outcomes = await asyncio.gather(
        ensure_officer_job_activated(db, db.job, provision=provision),
        ensure_officer_job_activated(db, db.job, provision=provision),
    )
    assert calls == 1
    assert sum(outcome.attempted for outcome in outcomes) == 1
    assert db.job["status"] == "created"
    assert db.job["id"] == outcomes[0].job_id == outcomes[1].job_id
