"""Manifest definition retirement distinguishes history from live authority."""

from datetime import datetime, timezone
from uuid import UUID

import pytest

from orchestrator.services.manifest_execution_retirement import (
    classify_execution_references,
)


EXECUTION = UUID("11111111-1111-4111-8111-111111111111")
WORK = UUID("22222222-2222-4222-8222-222222222222")
OWNER = UUID("33333333-3333-4333-8333-333333333333")


def session_spec(*, owner=None):
    return {
        "id": EXECUTION,
        "work_kind": "Session",
        "work_id": WORK,
        "owner_id": owner,
    }


def ended_thread(**changes):
    return {
        "id": WORK,
        "status": "ended",
        "user_id": None,
        "agent_id": None,
        "execution_lane": "pinned",
        "metadata": {},
        "runtime_authority_exposed": False,
        "runtime_attach_token": None,
        "runtime_retirement_token": None,
        "runtime_retirement_authorized_at": None,
        "runtime_retirement_stage_receipt": None,
        "runtime_retirement_local_quiescence": None,
        "runtime_retirement_external_cleanup": None,
        "control_admission_agent_id": None,
        **changes,
    }


class Connection:
    def __init__(self, *, spec, thread=None, job=None, attempts=(), extras=None):
        self.spec = spec
        self.thread = thread
        self.job = job
        self.attempts = list(attempts)
        self.extras = dict(extras or {})

    async def fetch(self, query, *args):
        if "FROM srw_execution_specs s" in query:
            return [self.spec]
        if "FROM jobs WHERE" in query:
            return [self.job] if self.job else []
        if "FROM threads WHERE" in query:
            return [self.thread] if self.thread else []
        if "FROM srw_execution_attempts" in query:
            return self.attempts
        if "FROM srw_workspace_instances" in query:
            return self.extras.get("direct_workspaces", [])
        if "FROM srw_execution_workspace_bindings" in query:
            return self.extras.get("bound_workspaces", [])
        if "FROM thread_agent_workspace_claims" in query:
            return self.extras.get("claims", [])
        if "FROM thread_workspace_provision_intents" in query:
            return self.extras.get("provision_intents", [])
        if "FROM cloud_ro_mounts" in query:
            return self.extras.get("protected_readers", [])
        if "FROM docker_workspace_leases" in query:
            return self.extras.get("docker_leases", [])
        if "FROM managed_repository_workspace_cleanup_intents" in query:
            return self.extras.get("cleanup_intents", [])
        if "FROM run_queue" in query:
            return self.extras.get("run_queue", [])
        raise AssertionError(query)


async def assessment(connection, *, retiring_owner_id=None):
    result = await classify_execution_references(
        connection,
        project_id=UUID("44444444-4444-4444-8444-444444444444"),
        retiring_owner_id=retiring_owner_id,
    )
    assert len(result) == 1
    return result[0]


@pytest.mark.asyncio
async def test_ownerless_authority_free_session_is_settled_history():
    result = await assessment(Connection(spec=session_spec(), thread=ended_thread()))
    assert result.state == "historical_settled"
    assert result.reason == "session_terminal_ownerless_history"
    assert not result.blocks_retirement


@pytest.mark.asyncio
async def test_ended_session_with_owner_remains_resumable():
    result = await assessment(
        Connection(
            spec=session_spec(owner=OWNER),
            thread=ended_thread(user_id=OWNER),
        )
    )
    assert result.state == "live"
    assert result.reason == "session_resumable_owner"


@pytest.mark.asyncio
async def test_owner_retirement_can_reclassify_only_its_settled_session():
    result = await assessment(
        Connection(
            spec=session_spec(owner=OWNER),
            thread=ended_thread(user_id=OWNER),
        ),
        retiring_owner_id=OWNER,
    )
    assert result.state == "historical_settled"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"runtime_retirement_token": OWNER}, "session_runtime_authority_unsettled"),
        (
            {"metadata": {"workspace_container": {"status": "deleted"}}},
            "session_metadata_authority_unsettled",
        ),
    ],
)
async def test_ended_ownerless_session_still_refuses_unsettled_authority(
    changes, reason
):
    result = await assessment(
        Connection(spec=session_spec(), thread=ended_thread(**changes))
    )
    assert result.state == "retirement_unsettled"
    assert result.reason == reason


@pytest.mark.asyncio
async def test_unclean_attempt_blocks_otherwise_terminal_job():
    spec = {
        "id": EXECUTION,
        "work_kind": "Job",
        "work_id": WORK,
        "owner_id": OWNER,
    }
    result = await assessment(
        Connection(
            spec=spec,
            job={"id": WORK, "status": "completed"},
            attempts=[
                {
                    "execution_id": EXECUTION,
                    "attempt": 1,
                    "phase": "Succeeded",
                    "cleaned_at": None,
                }
            ],
        )
    )
    assert result.state == "retirement_unsettled"
    assert result.reason == "execution_attempt_unsettled"


@pytest.mark.asyncio
async def test_cleaned_terminal_job_is_historical():
    spec = {
        "id": EXECUTION,
        "work_kind": "Job",
        "work_id": WORK,
        "owner_id": OWNER,
    }
    result = await assessment(
        Connection(
            spec=spec,
            job={"id": WORK, "status": "failed"},
            attempts=[
                {
                    "execution_id": EXECUTION,
                    "attempt": 1,
                    "phase": "Failed",
                    "cleaned_at": datetime.now(timezone.utc),
                }
            ],
        )
    )
    assert result.state == "historical_settled"
    assert result.reason == "job_terminal_history"
