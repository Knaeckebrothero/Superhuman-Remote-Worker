"""Verify the agent's register payload includes pod_uid from env.

The Kubernetes downward API in the agent pod manifest exports
``metadata.uid`` as the ``POD_UID`` env var. ``OrchestratorClient.register``
must lift this from os.environ and include it in the JSON body so the
orchestrator can persist it on the agents row (used by the session router
to set K8s ownerReferences on per-session Service/Ingress resources).
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.api.orchestrator_client import OrchestratorClient


@pytest.mark.asyncio
async def test_register_payload_includes_pod_uid_from_env(monkeypatch):
    monkeypatch.setenv("POD_UID", "k8s-pod-uid-deadbeef")

    client = OrchestratorClient(
        orchestrator_url="http://test-orch:8085",
        pod_ip="10.0.0.5",
        pod_port=8001,
        hostname="srw-agent-test",
        config_name="defaults",
        pid=1234,
    )

    fake_http = AsyncMock()
    fake_http.post = AsyncMock(
        return_value=MagicMock(
            status_code=200,
            json=lambda: {"agent_id": "a-1", "heartbeat_interval_seconds": 60},
        )
    )
    client._client = fake_http

    ok = await client.register(agent_mode="worker")

    assert ok is True
    # httpx is invoked as client.post(url, json=payload); read kwargs.
    payload = fake_http.post.call_args.kwargs["json"]
    assert payload["pod_uid"] == "k8s-pod-uid-deadbeef"


@pytest.mark.asyncio
async def test_register_payload_pod_uid_empty_when_env_unset(monkeypatch):
    """Outside K8s (local dev), POD_UID is unset; the field is an empty
    string rather than missing, so the orchestrator's pydantic model can
    treat it uniformly. The DB layer treats empty string as NULL on insert.
    """
    monkeypatch.delenv("POD_UID", raising=False)

    client = OrchestratorClient(
        orchestrator_url="http://test-orch:8085",
        pod_ip="10.0.0.5",
        pod_port=8001,
        hostname="srw-agent-test",
        config_name="defaults",
        pid=1234,
    )

    fake_http = AsyncMock()
    fake_http.post = AsyncMock(
        return_value=MagicMock(
            status_code=200,
            json=lambda: {"agent_id": "a-1", "heartbeat_interval_seconds": 60},
        )
    )
    client._client = fake_http

    ok = await client.register(agent_mode="worker")

    assert ok is True
    payload = fake_http.post.call_args.kwargs["json"]
    assert payload["pod_uid"] == ""


@pytest.mark.asyncio
async def test_register_raises_on_409_for_thread_bound():
    """A thread-bound registration that loses the provisioning race gets a 409
    ("thread already bound to another live agent"). register() must raise
    DuplicateThreadBinding so the dedicated-mode lifespan can exit the pod
    cleanly — leaving the per-session Service endpoints instead of lingering as
    an orphan that black-holes connections. See
    docs/done/persistent_thread_double_provisioning_race.md."""
    from src.api.orchestrator_client import DuplicateThreadBinding

    client = OrchestratorClient(
        orchestrator_url="http://test-orch:8085",
        pod_ip="10.0.0.5",
        pod_port=8001,
        hostname="srw-agent-loser",
        config_name="persistent_defaults",
        pid=1234,
    )

    fake_http = AsyncMock()
    fake_http.post = AsyncMock(
        return_value=MagicMock(
            status_code=409,
            text='{"detail":"thread already bound to another live agent"}',
        )
    )
    client._client = fake_http

    with pytest.raises(DuplicateThreadBinding):
        await client.register(
            agent_mode="persistent",
            thread_id="00000000-0000-0000-0000-000000000001",
        )


@pytest.mark.asyncio
async def test_register_409_without_thread_id_returns_false():
    """Worker/pool/dual registrations carry no thread_id and never trigger the
    lost-bind race; a 409 there returns False rather than raising."""
    client = OrchestratorClient(
        orchestrator_url="http://test-orch:8085",
        pod_ip="10.0.0.5",
        pod_port=8001,
        hostname="srw-agent-worker",
        config_name="defaults",
        pid=1234,
    )

    fake_http = AsyncMock()
    fake_http.post = AsyncMock(return_value=MagicMock(status_code=409, text="conflict"))
    client._client = fake_http

    ok = await client.register(agent_mode="worker")

    assert ok is False


@pytest.mark.asyncio
async def test_register_non_409_failure_returns_false():
    """A transient / 5xx failure on a thread-bound registration returns False
    (pod stays up, session-less) — only a 409 means a lost bind race."""
    client = OrchestratorClient(
        orchestrator_url="http://test-orch:8085",
        pod_ip="10.0.0.5",
        pod_port=8001,
        hostname="srw-agent-x",
        config_name="persistent_defaults",
        pid=1234,
    )

    fake_http = AsyncMock()
    fake_http.post = AsyncMock(
        return_value=MagicMock(status_code=503, text="unavailable")
    )
    client._client = fake_http

    ok = await client.register(
        agent_mode="persistent",
        thread_id="00000000-0000-0000-0000-000000000001",
    )

    assert ok is False
