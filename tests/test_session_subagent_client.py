"""Internal HTTP client contract for session-owned subagents."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.api.orchestrator_client import (
    OrchestratorClient,
    SubagentPersistenceError,
)
from shared.session_subagent_authority import (
    SessionParentAuthority,
    SessionParentAuthorityRefused,
    session_subagent_delivery_id,
)

PARENT = "aaaaaaaa-1111-4222-8333-444444444444"
OTHER_PARENT = "bbbbbbbb-1111-4222-8333-444444444444"
CHILD = "cccccccc-1111-4222-8333-444444444444"
GENERATION = "dddddddd-1111-4222-8333-444444444444"
AGENT = "eeeeeeee-1111-4222-8333-444444444444"
ATTACH = "ffffffff-1111-4222-8333-444444444444"


def _pinned() -> SessionParentAuthority:
    return SessionParentAuthority(
        execution_lane="pinned",
        parent_thread_id=PARENT,
        agent_id=AGENT,
        pod_uid="pod-uid",
        session_runtime_generation=GENERATION,
        runtime_attach_token=ATTACH,
    )


def _stateless() -> SessionParentAuthority:
    return SessionParentAuthority(
        execution_lane="stateless",
        parent_thread_id=PARENT,
        lease_token=3,
        executor_id="worker-1",
        executor_pod_uid="pod-uid",
    )


@pytest.fixture
def client() -> OrchestratorClient:
    value = OrchestratorClient(
        orchestrator_url="http://orchestrator:8085",
        pod_ip="10.0.0.5",
        pod_port=8002,
        hostname="session-agent",
        config_name="interactive",
    )
    value._client = MagicMock()
    value._client.post = AsyncMock()
    return value


def _response(status: int, payload: dict) -> MagicMock:
    response = MagicMock(status_code=status)
    response.json.return_value = payload
    response.text = str(payload)
    return response


@pytest.mark.asyncio
async def test_background_create_requires_exact_receipt_and_wire_authority(client):
    client._client.post.return_value = _response(
        200, {"thread_id": CHILD, "runtime_generation": GENERATION}
    )

    receipt = await client.create_session_subagent_thread(
        PARENT,
        parent_authority=_pinned(),
        subagent_id=CHILD,
        handle="reviewer-1a2b",
        subagent_type="reviewer",
        run_in_background=True,
        initial_status="queued",
    )

    assert receipt == {"thread_id": CHILD, "runtime_generation": GENERATION}
    call = client._client.post.await_args
    assert call.args[0].endswith(f"/threads/{PARENT}/subagents")
    assert call.kwargs["json"]["parent_authority"] == _pinned().to_wire()
    assert call.kwargs["json"]["run_in_background"] is True

    client._client.post.return_value = _response(
        200, {"thread_id": OTHER_PARENT, "runtime_generation": GENERATION}
    )
    with pytest.raises(SubagentPersistenceError):
        await client.create_session_subagent_thread(
            PARENT,
            parent_authority=_pinned(),
            subagent_id=CHILD,
            handle="reviewer-1a2b",
            subagent_type="reviewer",
            run_in_background=True,
            initial_status="queued",
        )


@pytest.mark.asyncio
async def test_foreground_create_retains_best_effort_payload_compatibility(client):
    client._client.post.return_value = _response(200, {"thread_id": CHILD})
    assert (
        await client.create_session_subagent_thread(
            PARENT,
            parent_authority=_pinned(),
            handle="reader-1a2b",
            subagent_type="reader",
        )
        is None
    )


@pytest.mark.asyncio
async def test_stateless_background_and_cross_parent_refuse_without_http(client):
    with pytest.raises(SessionParentAuthorityRefused) as background:
        await client.create_session_subagent_thread(
            PARENT,
            parent_authority=_stateless(),
            handle="reader-1a2b",
            subagent_type="reader",
            run_in_background=True,
            initial_status="queued",
        )
    assert background.value.reason == "stateless_background_unsupported"

    with pytest.raises(SessionParentAuthorityRefused) as mismatch:
        await client.list_live_session_subagent_threads(
            OTHER_PARENT, parent_authority=_pinned()
        )
    assert mismatch.value.reason == "parent_mismatch"

    malformed = {**_stateless().to_wire(), "lease_token": True}
    with pytest.raises(SessionParentAuthorityRefused) as invalid:
        await client.list_live_session_subagent_threads(
            PARENT, parent_authority=malformed
        )
    assert invalid.value.reason == "invalid"
    client._client.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_remote_authority_refusal_stays_typed(client):
    client._client.post.return_value = _response(
        409,
        {
            "detail": {
                "code": "session_parent_authority_refused",
                "reason": "pinned_parent_not_current",
            }
        },
    )
    with pytest.raises(SessionParentAuthorityRefused) as excinfo:
        await client.list_live_session_subagent_threads(
            PARENT, parent_authority=_pinned()
        )
    assert excinfo.value.reason == "pinned_parent_not_current"


@pytest.mark.asyncio
async def test_live_exact_and_by_call_paths_validate_payloads(client):
    live = _response(
        200,
        {
            "subagents": [
                {
                    "thread_id": CHILD,
                    "parent_job_id": None,
                    "parent_thread_id": PARENT,
                    "runtime_generation": GENERATION,
                }
            ]
        },
    )
    exact = _response(
        200,
        {
            "thread_id": CHILD,
            "parent_job_id": None,
            "parent_thread_id": PARENT,
            "runtime_generation": GENERATION,
        },
    )
    by_call = _response(
        200,
        {
            "thread_id": CHILD,
            "parent_job_id": None,
            "parent_thread_id": PARENT,
            "runtime_generation": GENERATION,
        },
    )
    client._client.post.side_effect = [live, exact, by_call]

    assert (
        len(
            await client.list_live_session_subagent_threads(
                PARENT, parent_authority=_pinned()
            )
        )
        == 1
    )
    assert (
        await client.get_session_subagent_thread(
            PARENT, CHILD, parent_authority=_pinned()
        )
    )["thread_id"] == CHILD
    assert (
        await client.get_session_subagent_thread_by_call(
            PARENT, "call-1", parent_authority=_pinned()
        )
    )["runtime_generation"] == GENERATION
    urls = [call.args[0] for call in client._client.post.await_args_list]
    assert urls[0].endswith("/subagents/live")
    assert urls[1].endswith(f"/subagents/{CHILD}")
    assert urls[2].endswith("/subagents/by-call")


@pytest.mark.asyncio
async def test_live_roster_rejects_cross_parent_or_duplicate_identity(client):
    row = {
        "thread_id": CHILD,
        "parent_job_id": None,
        "parent_thread_id": OTHER_PARENT,
        "runtime_generation": GENERATION,
    }
    client._client.post.return_value = _response(200, {"subagents": [row]})
    with pytest.raises(SubagentPersistenceError):
        await client.list_live_session_subagent_threads(
            PARENT, parent_authority=_pinned()
        )

    row["parent_thread_id"] = PARENT
    client._client.post.return_value = _response(200, {"subagents": [row, dict(row)]})
    with pytest.raises(SubagentPersistenceError):
        await client.list_live_session_subagent_threads(
            PARENT, parent_authority=_pinned()
        )


@pytest.mark.asyncio
async def test_background_terminal_omits_driver_id_and_validates_server_id(client):
    expected = str(session_subagent_delivery_id(CHILD, GENERATION))
    client._client.post.return_value = _response(
        200,
        {
            "result": "applied",
            "thread_id": CHILD,
            "runtime_generation": GENERATION,
            "delivery_id": expected,
            "delivery_state": "queued",
            "delivery": {"id": expected, "source": "subagent", "role": "event"},
        },
    )

    result = await client.terminalize_session_subagent_thread(
        PARENT,
        CHILD,
        parent_authority=_pinned(),
        runtime_generation=GENERATION,
        subagent_status="completed",
        run_in_background=True,
        message="child evidence",
    )

    assert result["delivery_id"] == expected
    payload = client._client.post.await_args.kwargs["json"]
    assert payload["delivery_id"] is None
    assert payload["message"] == "child evidence"

    client._client.post.return_value = _response(
        200,
        {
            "result": "idempotent",
            "thread_id": CHILD,
            "runtime_generation": GENERATION,
            "delivery_id": expected,
            "delivery_state": "settled",
        },
    )
    assert (
        await client.terminalize_session_subagent_thread(
            PARENT,
            CHILD,
            parent_authority=_pinned(),
            runtime_generation=GENERATION,
            subagent_status="completed",
            run_in_background=True,
            message="child evidence",
        )
    )["result"] == "idempotent"

    client._client.post.return_value = _response(
        200,
        {
            "result": "idempotent",
            "thread_id": CHILD,
            "runtime_generation": GENERATION,
            "delivery_id": OTHER_PARENT,
            "delivery_state": "queued",
        },
    )
    with pytest.raises(SubagentPersistenceError):
        await client.terminalize_session_subagent_thread(
            PARENT,
            CHILD,
            parent_authority=_pinned(),
            runtime_generation=GENERATION,
            subagent_status="completed",
            run_in_background=True,
            message="child evidence",
        )


@pytest.mark.asyncio
async def test_foreground_terminal_and_reopen_preserve_exact_receipts(client):
    terminal = _response(
        200,
        {
            "result": "applied",
            "thread_id": CHILD,
            "runtime_generation": GENERATION,
            "delivery_id": None,
            "delivery_state": None,
        },
    )
    reopen = _response(
        200,
        {
            "result": "reopened",
            "thread_id": CHILD,
            "runtime_generation": ATTACH,
        },
    )
    client._client.post.side_effect = [terminal, reopen]

    result = await client.terminalize_session_subagent_thread(
        PARENT,
        CHILD,
        parent_authority=_pinned(),
        runtime_generation=GENERATION,
        subagent_status="completed",
        run_in_background=False,
    )
    assert result["delivery_id"] is None
    assert client._client.post.await_args_list[0].kwargs["json"]["message"] is None

    result = await client.reopen_session_subagent_thread(
        PARENT,
        CHILD,
        parent_authority=_pinned(),
        runtime_generation=GENERATION,
    )
    assert result["runtime_generation"] == ATTACH


@pytest.mark.asyncio
async def test_foreground_orphan_recovery_carries_message_and_validates_delivery(
    client,
):
    expected = str(session_subagent_delivery_id(CHILD, GENERATION))
    client._client.post.return_value = _response(
        200,
        {
            "result": "applied",
            "thread_id": CHILD,
            "runtime_generation": GENERATION,
            "delivery_id": expected,
            "delivery_state": "queued",
        },
    )

    result = await client.terminalize_session_subagent_thread(
        PARENT,
        CHILD,
        parent_authority=_pinned(),
        runtime_generation=GENERATION,
        subagent_status="interrupted",
        run_in_background=False,
        message="partial evidence",
        outcome="interrupted:parent_restart",
        foreground_orphan_recovery=True,
    )

    assert result["delivery_id"] == expected
    payload = client._client.post.await_args.kwargs["json"]
    assert payload["message"] == "partial evidence"
    assert payload["foreground_orphan_recovery"] is True

    client._client.post.return_value = _response(
        200,
        {
            "result": "already_delivered",
            "thread_id": CHILD,
            "runtime_generation": GENERATION,
            "delivery_id": None,
            "delivery_state": None,
        },
    )
    assert (
        await client.terminalize_session_subagent_thread(
            PARENT,
            CHILD,
            parent_authority=_pinned(),
            runtime_generation=GENERATION,
            subagent_status="completed",
            run_in_background=False,
            message="unused after parent tool result",
            foreground_orphan_recovery=True,
        )
    )["result"] == "already_delivered"
