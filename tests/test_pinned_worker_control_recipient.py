"""Worker job mutations are bound to one exact registered runtime process.

``/system/shell-state`` already proves the read side of this fence
(``test_job_shell_state_recipient.py``). These are the *mutating* worker
endpoints: a same-IP successor that inherited the predecessor's Pod IP, and a
pre-contract orchestrator that sends no recipient at all, must both be refused
before the job is accepted or a cooperative stop is signalled.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from src.api.models import (
    JobCancelByOrchestratorRequest,
    JobResumeRequest,
    JobStartRequest,
)


AGENT_ID = "11111111-1111-4111-8111-111111111111"
JOB_ID = "22222222-2222-4222-8222-222222222222"
PROCESS_GENERATION = "33333333-3333-4333-8333-333333333333"
SUCCESSOR_GENERATION = "44444444-4444-4444-8444-444444444444"


def _recipient(*, process_generation: str = PROCESS_GENERATION) -> dict:
    return {
        "expected_agent_id": AGENT_ID,
        "expected_pod_uid": None,
        "expected_process_generation": process_generation,
        "expected_job_id": JOB_ID,
    }


@pytest.fixture(params=["app", "dual_app"])
def worker_runtime(request):
    """A registered worker process that already owns ``JOB_ID``."""

    if request.param == "app":
        from src.api import app as module

        application = module.create_app()
    else:
        from src.api import dual_app as module

        application = module.create_dual_app()

    saved = {
        name: getattr(module, name)
        for name in (
            "_agent",
            "_current_job_id",
            "_orchestrator_client",
            "_stop_requested",
            "_stop_completed",
        )
    }
    if request.param == "dual_app":
        saved["_pod_state"] = module._pod_state
        module._pod_state = module.PodState.WORKING
    module._agent = MagicMock()
    module._current_job_id = JOB_ID
    module._orchestrator_client = SimpleNamespace(
        agent_id=AGENT_ID,
        dispatch_process_generation=PROCESS_GENERATION,
    )
    # These are module-level asyncio.Events; a leftover binding from an earlier
    # test's loop would raise instead of exercising the fence.
    module._stop_requested = asyncio.Event()
    module._stop_completed = asyncio.Event()
    routes = {
        route.path: route.endpoint
        for route in application.routes
        if getattr(route, "path", "").startswith("/job/")
    }
    try:
        yield module, routes
    finally:
        for name, value in saved.items():
            setattr(module, name, value)


async def _call(module, routes, path, payload):
    endpoint = routes[path]
    if path in {"/job/start", "/job/resume"}:
        model = JobStartRequest if path == "/job/start" else JobResumeRequest
        kwargs = {"job_id": JOB_ID, "recipient": payload}
        if path == "/job/start":
            kwargs["description"] = "must not reach a foreign runtime"
        request = model(**kwargs)
        # ``app`` schedules background work, ``dual_app`` owns its own task.
        if module.__name__.endswith("dual_app"):
            return await endpoint(request)
        from fastapi import BackgroundTasks

        return await endpoint(request, BackgroundTasks())
    return await endpoint(JobCancelByOrchestratorRequest(recipient=payload))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path", ("/job/start", "/job/resume", "/job/cancel", "/job/pause")
)
@pytest.mark.parametrize("recipient", (None, "successor"))
async def test_worker_mutation_refuses_a_foreign_or_absent_recipient(
    worker_runtime, path, recipient
):
    module, routes = worker_runtime
    payload = (
        None
        if recipient is None
        else _recipient(process_generation=SUCCESSOR_GENERATION)
    )

    with pytest.raises(HTTPException) as refused:
        await _call(module, routes, path, payload)

    assert refused.value.status_code == 409
    assert refused.value.detail == {"code": "pinned_recipient_mismatch"}
    # No job accepted, no cooperative stop signalled, no task spawned.
    assert module._current_job_id == JOB_ID
    assert module._current_job_task is None
    assert not module._stop_requested.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ("/job/start", "/job/resume"))
async def test_exact_recipient_passes_the_fence(worker_runtime, path):
    """The fence must admit the registered process, not refuse everything.

    With the agent deliberately uninitialised, passing the fence surfaces as
    the downstream 503 — proof that control reached past the recipient check
    without the endpoint taking any job-accepting side effect.
    """

    module, routes = worker_runtime
    module._agent = None

    with pytest.raises(HTTPException) as outcome:
        await _call(module, routes, path, _recipient())

    assert outcome.value.status_code == 503
    assert module._current_job_task is None
    assert not module._stop_requested.is_set()
